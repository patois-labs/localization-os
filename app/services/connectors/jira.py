"""Jira connector — Jira Cloud REST API v3 with email + API-token (basic auth).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, SPEC, JIRA_SUMMARY_MAX_CHARS, JQL_GET_MAXLEN,
_TRANSLATABLE_CF_TYPES, _RICHTEXT_CF_SUFFIXES, _ADF_CF_SCHEMA_TYPES, _BUILTIN_TEXT_FIELDS,
_ADF_FIELDS, _safe_attachment_path, _cf_schema_kind, _LABEL_PREFIX, _adf_text, _adf_doc,
_META_FIELDS, _issue_meta, _WEBHOOK_SECRET_HEADER, _jira_header_secret_ok, _jira_signature_ok,
_jira_verifier, _ISSUE_KEY_RE, _jira_issue_keys, _NONCONTENT_CHANGELOG_FIELDS,
changelog_indicates_content_change, _attachment_allowlist, _translated_attachment_name,
_is_our_attachment_name, _attachment_bytes_hash, _build_attachment_bytes, _locale_is_held,
_jira_translate_attachments, JiraConnector from app.services.connectors.jira_part1,
app.services.connectors.jira_part2.

Config:  base_url, jql (or project_key), source_field, target_mode, target_field
Secret:  email, api_token

pull():  runs a JQL search and takes each issue's source_field (default 'summary')
         as a PulledItem keyed by the issue key.
push():  Jira issues are monolingual, so by default the translation is posted as a
         COMMENT on the issue (non-destructive). If target_mode='field', it's
         written into the configured target custom field instead (e.g. customfield_10050);
         the source summary/description are never overwritten, and field mode is
         refused (before any MT) without a target_field (Issue #641).

ELEMENT COVERAGE (kept honest against the element matrix):
  ✅ Summary          — plain string.
  ✅ Description      — ADF, via the real AST walker (app.filters.adf): leaf text
                        translated, marks/links/mentions/emoji/panels/tables/code
                        preserved; structure-integrity guard -> keep-original.
  ✅ Text custom fields — string / text-typed cf ids, editmeta-validated.
  ✅ Comments (write) — labelled-comment / field write-back (default, non-destructive).
  ✅ Comments (source, opt-in) — translate EXISTING issue comments as content when
                        translate_comments is enabled (off by default). Our OWN
                        labelled replies (see _LABEL_PREFIX) are filtered out of
                        fetch_comments so they never become translation units
                        themselves (no self-feedback loop).
  ➖ select / labels / number / date fields — NOT translated (skip-by-type).
  ✅ Attachments (OUTBOUND, opt-in) — translate the TEXT INSIDE attached files
                        (docx/pdf/html/…) through the shared file-filter pipeline and
                        attach the translated file back alongside the original
                        (additive, never an overwrite). Tunnel-FREE: download +
                        multipart upload both use the same token, no public URL.
                        Off by default (attachment_translate); a per-extension
                        allowlist + MAX_ATTACHMENT_BYTES cap gate what proceeds;
                        unsupported/over-cap files are skipped, never written blind.
                        Loop-guard: an attachment WE authored (author.accountId ==
                        our own) OR named like our own translation output
                        ({stem}.{locale}.{ext}) is never re-detected as source work.
                        See store.jira_translate_attachments. The Airtable/inbound
                        half stays tunnel/Fly-gated (SPEC-attachments Phase B).
  ➖ JSM request types / portal, board/project/issue-type names — UI i18n, not
                        per-issue content; out of scope.

FIELD-MODE CLOBBER WINDOW (F2, adversarial review) — READ BEFORE ENABLING field mode:
  Jira's REST API has NO optimistic-concurrency primitive for issue fields (no
  ETag / version check on PUT /issue/{key}). target_mode='field' PUTs a whole
  field value; if a human edits that SAME field between our fetch and our write,
  a plain PUT is last-write-wins and silently clobbers their edit. We narrow
  (never fully close) that window: immediately before the field PUT, we re-GET
  the field and compare it to the value we fetched at the start of the run. If
  it changed, we SKIP that field with a "concurrent edit detected" reason
  instead of writing — see JiraConnector._put_field's `expected_current` param
  and store.jira_translate's failures list. A change landing in the (now much
  shorter) re-GET-to-PUT gap is still possible in principle but not practically
  exploitable in the batch/translate-on-demand flows this connector is used from.
  Comment mode (the default) has no such window — comments are additive.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
import re

import httpx
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.filters import adf
from app.models import Document, Project, Segment, TransMode
from app.services import providers
from app.services.connectors import egress_guard, oauth
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
    _validate_target,
    harvest_context_field,
    reject_unwrappable_query,
    to_int,
)
from app.services.connectors.jira_part1 import (
    _ADF_CF_SCHEMA_TYPES,
    _ADF_FIELDS,
    _BUILTIN_TEXT_FIELDS,
    _ISSUE_KEY_RE,
    _LABEL_PREFIX,
    _META_FIELDS,
    _NONCONTENT_CHANGELOG_FIELDS,
    _RICHTEXT_CF_SUFFIXES,
    _TRANSLATABLE_CF_TYPES,
    _WEBHOOK_SECRET_HEADER,
    JIRA_SUMMARY_MAX_CHARS,
    JQL_GET_MAXLEN,
    SPEC,
    _adf_doc,
    _adf_text,
    _attachment_allowlist,
    _attachment_bytes_hash,
    _build_attachment_bytes,
    _cf_schema_kind,
    _is_our_attachment_name,
    _issue_meta,
    _jira_header_secret_ok,
    _jira_issue_keys,
    _jira_signature_ok,
    _jira_translate_attachments,
    _jira_verifier,
    _locale_is_held,
    _safe_attachment_path,
    _translated_attachment_name,
    changelog_indicates_content_change,
    log,
)
from app.services.connectors.jira_part2 import JiraConnector

__all__ = [
    "JIRA_SUMMARY_MAX_CHARS",
    "JQL_GET_MAXLEN",
    "SPEC",
    "_ADF_CF_SCHEMA_TYPES",
    "_ADF_FIELDS",
    "_BUILTIN_TEXT_FIELDS",
    "_ISSUE_KEY_RE",
    "_LABEL_PREFIX",
    "_META_FIELDS",
    "_NONCONTENT_CHANGELOG_FIELDS",
    "_RICHTEXT_CF_SUFFIXES",
    "_TRANSLATABLE_CF_TYPES",
    "_WEBHOOK_SECRET_HEADER",
    "Connector",
    "ConnectorError",
    "ConnectorSpec",
    "Document",
    "FieldSpec",
    "JiraConnector",
    "Project",
    "PulledItem",
    "Segment",
    "Session",
    "TransMode",
    "_adf_doc",
    "_adf_text",
    "_attachment_allowlist",
    "_attachment_bytes_hash",
    "_build_attachment_bytes",
    "_cf_schema_kind",
    "_is_our_attachment_name",
    "_issue_meta",
    "_jira_header_secret_ok",
    "_jira_issue_keys",
    "_jira_signature_ok",
    "_jira_translate_attachments",
    "_jira_verifier",
    "_locale_is_held",
    "_safe_attachment_path",
    "_translated_attachment_name",
    "_validate_target",
    "adf",
    "changelog_indicates_content_change",
    "config",
    "egress_guard",
    "engine",
    "harvest_context_field",
    "httpx",
    "log",
    "logging",
    "oauth",
    "providers",
    "re",
    "reject_unwrappable_query",
    "select",
    "to_int",
]


def _jira_changed_issue_keys(body_json) -> list[str]:
    """The generalized intake's changed-ids hook for Jira: issue keys the push
    names, BUT gated by the changelog -- a changelog that changed only non-content
    fields yields NO keys, so the intake ACKs it as a no-op (no re-translation, no
    pending recorded). A payload with no changelog passes through (hash gate
    backstops). The configured-field override is applied by the enqueue callback,
    which has the connection; here we use the default content set (summary/
    description) since this hook is connection-agnostic."""
    if not changelog_indicates_content_change(body_json):
        return []
    return _jira_issue_keys(body_json)


def _jira_event_id(body_json, headers):
    """Use the signed issue key and changelog id, else hash the body at the caller.

    The delivery identifier header is unsigned and cannot establish event identity.
    """
    keys = _jira_issue_keys(body_json)
    cl = (body_json or {}).get("changelog")
    cl_id = str((cl or {}).get("id") or "") if isinstance(cl, dict) else ""
    if keys and cl_id:
        return f"{keys[0]}:{cl_id}"
    return None


def _register_webhooks() -> None:
    """Register Jira on the generalized intake. Lazy import of webhooks (it imports
    store which imports connectors) -- same pattern as airtable._register_webhooks."""
    from app.services.connectors import webhooks

    webhooks.register_verifier("jira", _jira_verifier, event_id=_jira_event_id)
    webhooks.register_changed_ids("jira", _jira_changed_issue_keys)


_register_webhooks()


# --- Jira Phase 2: Automation/webhook push posture ------------------------------
# BUTTON-TRIGGER IS THE DEFAULT (the maintainer's confirmed posture): a push notification —
# an Automation "Send web request" or a raw REST webhook delivery — should NOT
# auto-translate unless the connection has explicitly opted in. So the intake
# records the changed issue key(s) as PENDING on the connection and surfaces them
# (connection page + "Translate pending now" action / CLI verb) instead of
# enqueueing paid MT. A connection that sets `auto_translate_on_change` ON flips to
# the sibling posture (a verified push enqueues a targeted jira_translate at once).
