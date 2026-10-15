"""Persistence + Localization OS integration for connectors.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _fernet, enc_secret, log, CONFIG_LOCK, SECRET_LOCK,
_REFRESH_LOCKS, _REFRESH_LOCKS_GUARD, _refresh_lock, dec_secret, auth_mode, list_connections, get,
_coerce_id, _confine_customer_id, _resolve_content_type_id, connection_project_archived,
WriteBudget, delete, _log, history, _kind_enabled, _token_fresh, _looks_like_auth_error,
_set_status, effective_locales, _add_missing_locales, _latest_doc, _airtable_retarget_error,
_airtable_scope_target, refresh_webhooks, register_airtable_webhook, unregister_airtable_webhook,
airtable_webhook_status,
check_airtable_webhook_health, consume_airtable_payloads, _public_base_url, _add_pending_records,
_jira_harvest_fields, _attachment_allowlist, _jira_translate_attachments, jira_auto_translate_on,
_canonical_locale, zendesk_translate, document360_translate, freshdesk_translate, zoho_translate,
intercom_translate, salesforce_translate, salesforce_rediscover_fields, notion_translate,
notion_resolve_link, helpscout_translate, helpscout_resolve_link, wordpress_resolve_link,
strapi_translate, contentful_translate, shopify_translate, hubspot_translate, akeneo_translate,
sitecore_translate,
is_oauth, set_oauth_secret, content_type_attachable, create, _UNSET, update, patch_config,
patch_config_merge, patch_config_nested, _CFG_LOCALE_RE, _PRUNABLE_HASH_MAPS, prune_orphaned_config,
prune_all_orphaned_config, _refuse_archived_sync, _refuse_disabled_sync, _refuse_disabled_kind,
build, ensure_fresh_token, build_fresh, _with_oauth_retry, test_connection, rotate_secret,
browse_containers_unsaved, primary_locale, _ensure_project, _new_connector_doc, jira_record_pending,
jira_pending, wordpress_translate, containers, schema_fields, pull_connection, push_connection,
dynamic_translate, airtable_create_records, airtable_delete_records, jira_translate, jira_count,
confluence_translate from app.services.connectors.store_common, app.services.connectors.store_part2,
app.services.connectors.store_part3, app.services.connectors.store_part4.

- Connection rows store non-secret config + Fernet-encrypted credentials.
- pull_connection() fetches source content and materializes it as a normal Loc
  Studio Project + Document + Segments, so the existing TM / MT / workflow /
  editor / LQA pipeline works on connector content with zero special-casing.
- push_connection() reads the translated segments back out and writes them to the
  external system via the connector.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime

from cryptography.fernet import Fernet
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import (
    Connection,
    ConnStatus,
    DocStatus,
    Document,
    Project,
    Segment,
    SegStatus,
    SyncLog,
    TargetLocale,
    TransMode,
    WebhookEvent,
)
from app.services import audit, ids, providers
from app.services.connectors import connector_class, egress_guard, harvest, oauth
from app.services.connectors.base import Connector, ConnectorError, PulledItem, content_hash
from app.services.connectors.store_common import (
    _REFRESH_LOCKS,
    _REFRESH_LOCKS_GUARD,
    CONFIG_LOCK,
    SECRET_LOCK,
    WriteBudget,
    _add_missing_locales,
    _add_pending_records,
    _airtable_retarget_error,
    _airtable_scope_target,
    _attachment_allowlist,
    _canonical_locale,
    _coerce_id,
    _confine_customer_id,
    _fernet,
    _jira_harvest_fields,
    _jira_translate_attachments,
    _kind_enabled,
    _latest_doc,
    _log,
    _looks_like_auth_error,
    _public_base_url,
    _refresh_lock,
    _resolve_content_type_id,
    _set_status,
    _token_fresh,
    airtable_webhook_status,
    akeneo_translate,
    auth_mode,
    bitbucket_translate,
    check_airtable_webhook_health,
    confluence_translate,
    connection_project_archived,
    consume_airtable_payloads,
    contentful_translate,
    dec_secret,
    delete,
    document360_translate,
    drupal_translate,
    effective_locales,
    enc_secret,
    freshdesk_translate,
    gdocs_impersonation_error,
    gdocs_translate,
    get,
    github_translate,
    gitlab_translate,
    helpscout_resolve_link,
    helpscout_translate,
    history,
    hubspot_translate,
    intercom_translate,
    jira_auto_translate_on,
    languages_project_id,
    list_connections,
    mcp_cms_translate,
    msgraph_translate,
    notion_resolve_link,
    notion_translate,
    plytix_translate,
    refresh_webhooks,
    register_airtable_webhook,
    salesforce_rediscover_fields,
    salesforce_translate,
    sanity_translate,
    scope_admin_error,
    servicenow_resolve_link,
    servicenow_translate,
    shopify_translate,
    sitecore_translate,
    strapi_translate,
    unregister_airtable_webhook,
    wordpress_resolve_link,
    zendesk_translate,
    zoho_translate,
)
from app.services.connectors.store_part2 import (
    _CFG_LOCALE_RE,
    _PRUNABLE_HASH_MAPS,
    _UNSET,
    _ensure_project,
    _new_connector_doc,
    _refuse_archived_sync,
    _refuse_disabled_kind,
    _refuse_disabled_sync,
    _with_oauth_retry,
    browse_containers_unsaved,
    build,
    build_fresh,
    content_type_attachable,
    create,
    ensure_fresh_token,
    is_oauth,
    jira_pending,
    jira_record_pending,
    patch_config,
    patch_config_merge,
    patch_config_nested,
    primary_locale,
    prune_all_orphaned_config,
    prune_orphaned_config,
    rotate_secret,
    set_oauth_secret,
    test_connection,
    update,
    wordpress_translate,
)
from app.services.connectors.store_part3 import (
    airtable_create_records,
    containers,
    dynamic_translate,
    pull_connection,
    push_connection,
    schema_fields,
)
from app.services.connectors.store_part4 import airtable_delete_records, jira_count, jira_translate

__all__ = [
    "CONFIG_LOCK",
    "SECRET_LOCK",
    "_CFG_LOCALE_RE",
    "_PRUNABLE_HASH_MAPS",
    "_REFRESH_LOCKS",
    "_REFRESH_LOCKS_GUARD",
    "_UNSET",
    "Connector",
    "ConnectorError",
    "DocStatus",
    "Document",
    "Fernet",
    "Project",
    "PulledItem",
    "SegStatus",
    "Segment",
    "SyncLog",
    "TargetLocale",
    "TransMode",
    "WebhookEvent",
    "WriteBudget",
    "_add_missing_locales",
    "_add_pending_records",
    "_airtable_retarget_error",
    "_airtable_scope_target",
    "_attachment_allowlist",
    "_canonical_locale",
    "_coerce_id",
    "_confine_customer_id",
    "_ensure_project",
    "_fernet",
    "_jira_harvest_fields",
    "_jira_translate_attachments",
    "_kind_enabled",
    "_latest_doc",
    "_log",
    "_looks_like_auth_error",
    "_new_connector_doc",
    "_public_base_url",
    "_refresh_lock",
    "_refuse_archived_sync",
    "_refuse_disabled_kind",
    "_refuse_disabled_sync",
    "_resolve_content_type_id",
    "_set_status",
    "_token_fresh",
    "_with_oauth_retry",
    "airtable_create_records",
    "airtable_delete_records",
    "airtable_webhook_status",
    "akeneo_translate",
    "audit",
    "auth_mode",
    "bitbucket_translate",
    "browse_containers_unsaved",
    "build",
    "build_fresh",
    "check_airtable_webhook_health",
    "config",
    "confluence_translate",
    "connection_project_archived",
    "connector_class",
    "consume_airtable_payloads",
    "containers",
    "content_hash",
    "content_type_attachable",
    "contentful_translate",
    "create",
    "datetime",
    "dec_secret",
    "delete",
    "document360_translate",
    "drupal_translate",
    "dynamic_translate",
    "effective_locales",
    "egress_guard",
    "enc_secret",
    "ensure_fresh_token",
    "freshdesk_translate",
    "gdocs_impersonation_error",
    "gdocs_translate",
    "get",
    "github_translate",
    "gitlab_translate",
    "harvest",
    "helpscout_resolve_link",
    "helpscout_translate",
    "history",
    "hubspot_translate",
    "ids",
    "intercom_translate",
    "is_oauth",
    "jira_auto_translate_on",
    "jira_count",
    "jira_pending",
    "jira_record_pending",
    "jira_translate",
    "json",
    "languages_project_id",
    "list_connections",
    "mcp_cms_translate",
    "msgraph_translate",
    "notion_resolve_link",
    "notion_translate",
    "oauth",
    "patch_config",
    "patch_config_merge",
    "patch_config_nested",
    "plytix_translate",
    "primary_locale",
    "prune_all_orphaned_config",
    "prune_orphaned_config",
    "pull_connection",
    "push_connection",
    "re",
    "refresh_webhooks",
    "register_airtable_webhook",
    "rotate_secret",
    "salesforce_rediscover_fields",
    "salesforce_translate",
    "sanity_translate",
    "schema_fields",
    "scope_admin_error",
    "select",
    "servicenow_resolve_link",
    "servicenow_translate",
    "set_oauth_secret",
    "shopify_translate",
    "sitecore_translate",
    "strapi_translate",
    "test_connection",
    "threading",
    "unregister_airtable_webhook",
    "update",
    "wordpress_resolve_link",
    "wordpress_translate",
    "zendesk_translate",
    "zoho_translate",
]


log = logging.getLogger("loc_tms.connectors")


def sync_connection(cid: int, actor: str = "", transport=None) -> tuple[bool, str, dict]:
    """The automated round-trip — pull → auto-translate every segment with the
    project's engine (TM-first, glossary-aware, failover) → push — with no human
    step. This is what a one-click "Translate" button (in Localization OS or inside
    Airtable/Jira) invokes, so content is localized without opening Localization OS."""
    from app.services import translate

    _dis = _refuse_disabled_sync(cid, actor=actor)  # ffadmin Wave 5 (before any egress/bookkeeping)
    if _dis is not None:
        return False, _dis, {"document_id": None}
    _arch = _refuse_archived_sync(cid, actor=actor)  # M6/R1
    if _arch is not None:
        return True, _arch, {"document_id": None, "skipped_archived": True}
    ok, msg, doc_id = pull_connection(cid, actor=actor, transport=transport)
    if not ok or not doc_id:
        return ok, f"Pull step: {msg}", {"document_id": doc_id}

    conn = get(cid)
    target = primary_locale(conn)
    try:
        with Session(engine) as s:
            # Phase 2: let pretranslate resolve the provider per (project, this pair) —
            # UNIFORM projects behave exactly as before (project default), a PER_PAIR
            # project translates this connector's target with the pair's own engine.
            stats = translate.pretranslate_document(s, doc_id, target)
    except providers.DeadEngineError as e:
        # A stored engine:<id> ref went dead (deleted/disabled/keyless): FAIL the
        # sync LOUD with the clear message instead of writing mock placeholder
        # output back to the source system. Mark the connection ERROR so the UI
        # shows it, and record the failure in the connector log.
        msg = str(e)
        _set_status(cid, ConnStatus.ERROR, msg, synced=False)
        _log(cid, "sync", False, 0, msg, actor)
        return False, msg, {"document_id": doc_id}
    translated = stats.get("translated", 0)

    ok2, msg2, n = push_connection(cid, actor=actor, transport=transport)
    summary = {"document_id": doc_id, "translated": translated, "pushed": n}
    _log(cid, "sync", ok2, n, f"translated {translated}, pushed {n}", actor)
    if ok2:
        return (
            True,
            f"Automated sync complete — translated {translated} item(s), pushed {n} back.",
            summary,
        )
    return False, f"Translated {translated}, but push failed: {msg2}", summary


def jira_translate_pending(cid: int, actor: str = "", transport=None) -> tuple[bool, str, dict]:
    """Release the connection's PENDING issue keys: translate exactly those issues
    (via jira_translate) and clear the pending set on success. The "Translate
    pending now" action (web button + CLI verb) — the deliberate human step that
    button-trigger posture requires before any paid MT runs. A no-op (ok, nothing
    pending) when the set is empty. The pending set is cleared only for the keys we
    actually attempted, so a push that lands DURING the run is preserved."""
    conn = get(cid)
    if not conn or conn.kind != "jira":
        return False, "No such Jira connection.", {}
    pending = list((conn.config or {}).get("jira_pending") or [])
    if not pending:
        return True, "No pending issues to translate.", {"pending": 0, "written": 0}
    ok, msg, summary = jira_translate(cid, issue_keys=pending, actor=actor, transport=transport)
    # Issue #446: "ok" only means something was written. Clear just the keys the run
    # really HANDLED: not an issue whose pair failed / was cut by the write ceiling / was
    # held back for review, not a key the fetch cap never reached, and nothing at all when
    # the project is archived and the run was skipped (pending is the only queue then).
    summary = dict(summary or {})
    if ok and not summary.get("skipped_archived"):
        # Remove ONLY the keys we just processed; a concurrent push may have added
        # more since we read the list (read-merge-write under CONFIG_LOCK).
        done = set(pending) - set(summary.get("retain_keys") or [])
        with CONFIG_LOCK, Session(engine) as s:
            row = s.get(Connection, cid)
            if row:
                cfg = dict(row.config or {})
                cfg["jira_pending"] = [k for k in (cfg.get("jira_pending") or []) if k not in done]
                row.config = cfg
                s.add(row)
                s.commit()
    summary["pending"] = len(pending)
    return ok, msg, summary


def _epoch(ts) -> int:
    """Best-effort epoch seconds from a Zendesk ISO-8601 timestamp — WITH or
    WITHOUT fractional seconds ('2026-07-05T12:00:00Z', '...T12:00:00.123Z'),
    any UTC-offset form. fromisoformat (3.11+) handles all of these; a fixed
    strptime format would silently return 0 on the fractional form and freeze
    the watermark forever (review finding). Non-parseable -> 0 (never crashes
    the run; a frozen-at-0 watermark just means the backstop stays off)."""
    if not ts:
        return 0
    if isinstance(ts, (int, float)):
        return int(ts)
    try:
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        dt = _dt.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:  # naive -> treat as UTC
            dt = dt.replace(tzinfo=_tz.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


# Connectors whose one-click flow is exactly `<kind>_translate(cid, actor, transport)`.
# Each name resolves to a module global — a thin delegator for the relocated T2/T3
# flows, or the in-store jira/confluence flow — looked up dynamically so a test that
# monkeypatches store.<kind>_translate still wins.
_TRANSLATE_KINDS = (
    "jira",
    "confluence",
    "zendesk",
    "document360",
    "freshdesk",
    "zoho",
    "intercom",
    "salesforce",
    "notion",
    "gdocs",
    "helpscout",
    "strapi",
    "akeneo",
    "mcp_cms",
    "servicenow",
    "msgraph",
    "plytix",
    "drupal",
    "github",
    "sanity",
    "sitecore",
    "bitbucket",
    "gitlab",
    # factory:anchor store.translate_kinds
    "contentful",
    "shopify",
    "hubspot",
    "wordpress",
)


def translate_now(cid: int, actor: str = "", transport=None) -> tuple[bool, str, dict]:
    """The one-click 'Translate now' action — dispatches to the right flow per kind.
    Every path fans out to all of the connection's target locales (its Language Set
    or target_locales)."""
    conn = get(cid)
    if not conn:
        return False, "No such connection.", {}
    _dis = _refuse_disabled_sync(cid, actor=actor)  # ffadmin Wave 5 (before any egress/bookkeeping)
    if _dis is not None:
        return False, _dis, {}
    _arch = _refuse_archived_sync(cid, actor=actor)  # M6/R1
    if _arch is not None:
        return True, _arch, {"created": 0, "updated": 0, "skipped_archived": True}
    if conn.kind in _TRANSLATE_KINDS:
        return globals()[f"{conn.kind}_translate"](cid, actor=actor, transport=transport)
    # Airtable: multi-locale via the column naming convention (auto-detects every
    # target-language column) when base/table/source are configured.
    cfg = conn.config or {}
    if cfg.get("base_id") and cfg.get("table") and cfg.get("source_field"):
        return dynamic_translate(cid, actor=actor, transport=transport)
    return sync_connection(cid, actor=actor, transport=transport)


# Article-style retry connectors -> the "nothing to retry" message for each (only
# Notion phrases it as "page ids"). All call `<kind>_translate(article_ids=..., ...)`.
_RETRY_ARTICLE_KINDS = {
    "zendesk": "No article ids in the failed items.",
    "document360": "No article ids in the failed items.",
    "freshdesk": "No article ids in the failed items.",
    "zoho": "No article ids in the failed items.",
    "intercom": "No article ids in the failed items.",
    "salesforce": "No article ids in the failed items.",
    "notion": "No page ids in the failed items.",
    "confluence": "No page ids in the failed items.",
    "helpscout": "No article ids in the failed items.",
    "strapi": "No entry ids in the failed items.",
    "gdocs": "No document ids in the failed items.",
    "contentful": "No entry ids in the failed items.",
    "shopify": "No resource ids in the failed items.",
    "hubspot": "No object ids in the failed items.",
    "wordpress": "No post ids in the failed items.",
    "akeneo": "No product ids in the failed items.",
    "mcp_cms": "No item ids in the failed items.",
    "servicenow": "No article ids in the failed items.",
    "msgraph": "No file ids in the failed items.",
    "plytix": "No product ids in the failed items.",
    "drupal": "No entity ids in the failed items.",
    "github": "No file ids in the failed items.",
    "sanity": "No document ids in the failed items.",
    "sitecore": "No item ids in the failed items.",
    "bitbucket": "No file ids in the failed items.",
    "gitlab": "No file ids in the failed items.",
    # factory:anchor store.retry_article_kinds
}


def retry_failed(
    cid: int, failures: list[dict], actor: str = "", transport=None
) -> tuple[bool, str, dict]:
    """Re-run only the items that failed in a prior run.

    `failures` is a summary["failures"] list (see jira_translate /
    confluence_translate / dynamic_translate). It's grouped by shape and
    dispatched with the narrowing kwargs the flows already accept — so a retry
    touches ONLY the failed subset, never the whole source again:
      - airtable  -> dynamic_translate(record_ids=[...])
      - jira      -> jira_translate(issue_keys=[...], only_pairs={(key, loc)})
      - confluence-> confluence_translate(page_ids=[...], only_pairs={(page, loc)})
    For Jira and Confluence the WRITE loops previously wrote the full
    keys x locales cross product, double-posting pairs that had already
    succeeded; retry_failed now builds the exact (id, locale) pairs from the
    failure list and passes them as only_pairs, so a retry re-posts ONLY the
    failed pairs. (Confluence child-page dedup is a second guard.) Every field
    read is defensive so a malformed failure entry can't crash the retry."""
    conn = get(cid)
    if not conn:
        return False, "No such connection.", {}
    if not failures:
        return False, "No failed items to retry.", {}

    def _locales(items) -> list[str]:
        seen: list[str] = []
        for f in items:
            loc = str((f.get("locale") if isinstance(f, dict) else "") or "").strip()
            if loc and loc not in seen:
                seen.append(loc)
        return seen

    # LOW-4: the flow.run_translate_flow connectors (helpscout + every other T3/T2 flow)
    # LOWERCASE their usable locales, so their only_pairs membership test uses a
    # lowercased locale; a hand-built or mixed-case failure ('de-DE') would never match
    # and the retry would silently no-op. The legacy raw-locale loops (jira/confluence)
    # compare the locale verbatim, so they must keep it as-is (else THEIR retry breaks).
    _flow_lowercases = conn.kind != "jira"

    def _pairs(id_key: str) -> set:
        """Exact (id, locale) pairs from the failures — the retry write filter. The
        locale is lowercased for the flow connectors (which lowercase their usable
        locales) and kept verbatim for the raw-locale jira loop (LOW-4)."""
        out = set()
        for f in failures:
            if not isinstance(f, dict):
                continue
            # `page_id` is the key failures carried before Confluence moved onto the flow engine
            fid = str(
                f.get(id_key) or (f.get("page_id") if id_key == "article_id" else "") or ""
            ).strip()
            loc = str(f.get("locale") or "").strip()
            if fid and loc:
                out.add((fid, loc.lower() if _flow_lowercases else loc))
        return out

    if conn.kind == "jira":
        keys = sorted(
            {
                str(f.get("issue_key")).strip()
                for f in failures
                if isinstance(f, dict) and (f.get("issue_key") or "")
            }
        )
        if not keys:
            return False, "No issue keys in the failed items.", {}
        return jira_translate(
            cid,
            issue_keys=keys,
            target_locales=_locales(failures) or None,
            only_pairs=_pairs("issue_key") or None,
            actor=actor,
            transport=transport,
        )
    if conn.kind == "airtable":
        recs = sorted(
            {
                str(f.get("record_id")).strip()
                for f in failures
                if isinstance(f, dict) and (f.get("record_id") or "")
            }
        )
        if not recs:
            return False, "No record ids in the failed items.", {}
        return dynamic_translate(cid, record_ids=recs, actor=actor, transport=transport)
    # The article-style flows (every relocated T2/T3 connector) share one retry shape:
    # article_id-keyed ids + only_pairs, calling `<kind>_translate` looked up dynamically
    # (so a monkeypatched store.<kind>_translate still wins). Only the empty-set message
    # varies (Notion says "page ids"); the call is otherwise byte-identical per kind.
    missing_msg = _RETRY_ARTICLE_KINDS.get(conn.kind)
    if missing_msg is not None:
        arts = sorted(
            {
                str(f.get("article_id") or f.get("page_id")).strip()
                for f in failures
                if isinstance(f, dict) and (f.get("article_id") or f.get("page_id"))
            }
        )
        if not arts:
            return False, missing_msg, {}
        pairs = _pairs("article_id")
        # only_pairs is the ONLY retry signal the flow engine needs (Contentful review M1,
        # Shopify HIGH-1): a partially-written / already-fully-covered pair RECORDED a
        # content hash, so a plain run would hash-suppress it — but run_translate_flow
        # BYPASSES suppression for exactly these operator-requested pairs (flow.py stage 6),
        # so the pair re-materializes and re-attempts with NO per-connector hash surgery
        # here. This replaced the old contentful/shopify/hubspot stored-hash neutralisation:
        # the bypass now lives once, in the flow, for every connector. One-shot, operator-
        # triggered — the run re-records the hash on success, so there is no loop.
        return globals()[f"{conn.kind}_translate"](
            cid,
            article_ids=arts,
            target_locales=_locales(failures) or None,
            only_pairs=pairs or None,
            actor=actor,
            transport=transport,
        )
    return False, f"Retry not supported for {conn.kind} connections.", {}
