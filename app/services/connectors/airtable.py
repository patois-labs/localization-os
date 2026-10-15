"""Airtable connector — Airtable Web API v0 with a Personal Access Token.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports API, _LOC, _MENTION, _MENTION_OPEN, _MENTION_CLOSE,
_RAW_MENTION_SENTINEL, _MENTION_PLACEHOLDER, shield_mentions, unshield_mentions, _RICHTEXT_TYPES,
_LONGTEXT_TYPES, _PLAINTEXT_TYPES, field_kind, _TEXT_WRITABLE_TYPES, _SELECT_TYPES,
is_text_writable, is_select, is_single_select_writable, select_option_names, webhook_error_state,
_MD_CODE, _MD_LINK_URL, _MD_EMPH, _RAW_MD_SENTINEL, _shield_rich_markup, _mk_group,
_RESIDUAL_MENTION_PLACEHOLDER, _RESIDUAL_MD_PLACEHOLDER, _RESIDUAL_MD_NUL,
_strip_write_egress_debris, parse_webhook_cell, parse_webhook_changed, payloads_next_cursor,
payloads_might_have_more, detect_targets, _ALL_FROM_SOURCES, WRITEBACK_SOURCE,
FROM_SOURCES_EXCLUDING_WRITEBACK, webhook_spec, _airtable_mac_verifier, _airtable_event_id, SPEC,
AirtableConnector from app.services.connectors.airtable_part1,
app.services.connectors.airtable_part2.

Config:  base_id, table, source_field, target_field, view (optional), max_records
Secret:  api_key  (a PAT with data.records:read/write and, for picking, schema.bases:read)

pull():  reads records from {base}/{table}; each record's source_field becomes a
         PulledItem keyed by (record_id, target_field) so push() knows where to
         write the translation.
push():  PATCHes the records, writing each translation into target_field (batched
         in groups of 10, the API limit).

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import re

from app import branding
from app.services.connectors.airtable_part1 import (
    _ALL_FROM_SOURCES,
    _LOC,
    _LONGTEXT_TYPES,
    _MD_CODE,
    _MD_EMPH,
    _MD_LINK_URL,
    _MENTION,
    _MENTION_CLOSE,
    _MENTION_OPEN,
    _MENTION_PLACEHOLDER,
    _PLAINTEXT_TYPES,
    _RAW_MD_SENTINEL,
    _RAW_MENTION_SENTINEL,
    _RESIDUAL_MD_NUL,
    _RESIDUAL_MD_PLACEHOLDER,
    _RESIDUAL_MENTION_PLACEHOLDER,
    _RICHTEXT_TYPES,
    _SELECT_TYPES,
    _TEXT_WRITABLE_TYPES,
    API,
    FROM_SOURCES_EXCLUDING_WRITEBACK,
    WRITEBACK_SOURCE,
    _airtable_event_id,
    _airtable_mac_verifier,
    _mk_group,
    _shield_rich_markup,
    _strip_write_egress_debris,
    detect_targets,
    field_kind,
    is_select,
    is_single_select_writable,
    is_text_writable,
    parse_webhook_cell,
    parse_webhook_changed,
    payloads_might_have_more,
    payloads_next_cursor,
    select_option_names,
    shield_mentions,
    unshield_mentions,
    webhook_error_state,
    webhook_spec,
)
from app.services.connectors.airtable_part2 import SPEC, AirtableConnector
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
    _validate_target,
    harvest_context_field,
    to_int,
)

__all__ = [
    "API",
    "FROM_SOURCES_EXCLUDING_WRITEBACK",
    "SPEC",
    "WRITEBACK_SOURCE",
    "_ALL_FROM_SOURCES",
    "_LOC",
    "_LONGTEXT_TYPES",
    "_MD_CODE",
    "_MD_EMPH",
    "_MD_LINK_URL",
    "_MENTION",
    "_MENTION_CLOSE",
    "_MENTION_OPEN",
    "_MENTION_PLACEHOLDER",
    "_PLAINTEXT_TYPES",
    "_RAW_MD_SENTINEL",
    "_RAW_MENTION_SENTINEL",
    "_RESIDUAL_MD_NUL",
    "_RESIDUAL_MD_PLACEHOLDER",
    "_RESIDUAL_MENTION_PLACEHOLDER",
    "_RICHTEXT_TYPES",
    "_SELECT_TYPES",
    "_TEXT_WRITABLE_TYPES",
    "AirtableConnector",
    "Connector",
    "ConnectorError",
    "ConnectorSpec",
    "FieldSpec",
    "PulledItem",
    "_airtable_event_id",
    "_airtable_mac_verifier",
    "_mk_group",
    "_shield_rich_markup",
    "_strip_write_egress_debris",
    "_validate_target",
    "branding",
    "detect_targets",
    "field_kind",
    "harvest_context_field",
    "is_select",
    "is_single_select_writable",
    "is_text_writable",
    "parse_webhook_cell",
    "parse_webhook_changed",
    "payloads_might_have_more",
    "payloads_next_cursor",
    "select_option_names",
    "shield_mentions",
    "to_int",
    "unshield_mentions",
    "webhook_error_state",
    "webhook_spec",
]


_MD_PLACEHOLDER = re.compile("\x00MD(\\d+)\x00")


def encode_cell(value: str, kind: str) -> tuple[str, dict]:
    """Prepare a cell value for MT. Returns (mt_text, ctx). `kind` is from
    field_kind(). Mentions are ALWAYS shielded; rich fields also shield inline
    Markdown markup. ctx carries the ordered token lists for decode_cell()."""
    text = value or ""
    shielded, mentions = shield_mentions(text)
    md_tokens: list[str] = []
    if kind == "rich":
        shielded, md_tokens = _shield_rich_markup(shielded)
    return shielded, {"mentions": mentions, "md": md_tokens, "kind": kind}


def decode_cell(mt_text: str, ctx: dict) -> str:
    """Inverse of encode_cell: restore Markdown markup then mentions. Order is the
    reverse of encoding (markup was shielded after mentions, so restore it first)."""
    text = mt_text or ""
    md_tokens = (ctx or {}).get("md") or []
    if md_tokens:

        def _restore_md(m: re.Match) -> str:
            try:
                return md_tokens[int(m.group(1))]
            except (ValueError, IndexError):
                return m.group(0)

        text = _MD_PLACEHOLDER.sub(_restore_md, text)
    text = unshield_mentions(text, (ctx or {}).get("mentions") or [])
    return _strip_write_egress_debris(text)


def webhook_changed_record_ids(payload: dict) -> list[str]:
    """Just the record ids a webhook payload names (THIN-payload intake contract —
    the job re-fetches content). Deduped, order-preserving."""
    ids: list[str] = []
    for rec in parse_webhook_changed(payload):
        rid = str(rec.get("record_id") or "").strip()
        if rid and rid not in ids:
            ids.append(rid)
    return ids


def webhook_source_changed_record_ids(
    payload: dict, source_field_ids: set[str] | None = None
) -> list[str]:
    """Record ids from a `list payloads` response whose SOURCE field actually
    changed — the "translate on source change" filter for the ping-consumption job.

    When `source_field_ids` is given (the connection's watched source column(s)),
    a record is kept ONLY if at least one of those field ids appears in its
    changed `cellValuesByFieldId`. This drops TARGET-column echoes (our own
    write-backs land in the target column, whose field id is NOT in the source set)
    at the payload layer — a cheap, precise complement to (a) the webhook spec's
    `fromSources` excluding publicApi and (b) the store's written-hash backstop.

    When `source_field_ids` is empty/None (we don't know the field id — e.g. the
    connection watches by name and the meta lookup failed), we CANNOT tell a source
    change from a target echo here, so we return every changed record id and let the
    downstream written-hash guard + name-based source read decide. Deduped,
    order-preserving. Defensive: a malformed payload yields []."""
    src = source_field_ids or set()
    ids: list[str] = []
    for rec in parse_webhook_changed(payload):
        rid = str(rec.get("record_id") or "").strip()
        if not rid or rid in ids:
            continue
        if src:
            changed = set((rec.get("fields") or {}).keys())
            if not (changed & src):
                continue  # only target/other columns changed -> skip (echo)
        ids.append(rid)
    return ids


# Register the verifier + the thin-payload changed-ids extractor on the generalized
# intake. This is ADDITIVE: the existing convention endpoint
# (/api/connectors/{cid}/airtable/webhook, connector-token authed) is untouched for
# back-compat. The intake is a ping-then-pull shape — the ping body Airtable POSTs
# carries only {base, webhook} ids (no cell content), so the changed-ids extractor
# handles BOTH the tiny ping (no ids -> the job re-fetches payloads) and a
# list-payloads shape fed through parse_webhook_changed for tests.
def _airtable_changed_ids(body_json) -> list[str]:
    """Record ids from a verified Airtable webhook body. The real notification ping
    carries NO record ids (just base+webhook), so this returns [] for a ping and
    the enqueued job re-fetches payloads via list-payloads. When a payloads-shaped
    body is fed (tests / a future inline mode), extract ids via the V2 parser."""
    body_json = body_json or {}
    if "payloads" in body_json:
        return webhook_changed_record_ids(body_json)
    return []


def _register_webhooks() -> None:
    """Register Airtable on the generalized intake. Lazy import of webhooks to avoid
    an import cycle (webhooks imports store which imports connectors)."""
    from app.services.connectors import webhooks

    webhooks.register_verifier(
        "airtable",
        _airtable_mac_verifier,
        # The ping body carries no stable provider event id, so let dedup_key fall
        # through to a SHA-256 of the raw body — a true retry (byte-identical ping)
        # dedups; two distinct pings hash differently. A live tenant's ping DOES
        # carry a `timestamp`; prefer (webhook id, timestamp) when present so two
        # coalesced pings at the same instant still collapse but distinct ones don't.
        event_id=lambda body_json, headers: _airtable_event_id(body_json),
    )
    webhooks.register_changed_ids("airtable", _airtable_changed_ids)
    # Airtable is PING-THEN-PULL: a real notification carries no record ids, so the
    # intake must still enqueue a payload-pull job (store.consume_airtable_payloads)
    # on an empty-id ping. [CONFIRMED — research §2: notification body is tiny;
    # payloads are fetched separately by cursor.]
    webhooks.register_ping_pull("airtable")


_register_webhooks()
