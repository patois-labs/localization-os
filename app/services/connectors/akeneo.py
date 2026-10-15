"""Akeneo PIM connector — a T1 (field-level map) consumer of the connector framework.
Reference: bb-io/Akeneo (MIT).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, API_VERSION, _API_PREFIX, _ENDPOINT, _ID_FIELD,
_CONTENT_TYPES, _TRANSLATABLE_TYPES, _HELD_BACK_PREFIX, _HELD_BACK, _parse_names, _validate_segment,
_PatchOutcome, _Unit, SPEC, AkeneoProductStrategy, AkeneoConnector, _akeneo_preflight,
_akeneo_partition, _akeneo_prepare, _akeneo_is_suppressed, _akeneo_build_payload, _is_held_back,
_held_back, _real_failures, _dropped_as_source, _akeneo_compute_ok, _akeneo_build_message,
_akeneo_build_result from app.services.connectors.akeneo_part1.

TOPOLOGY — T1 (field-level map), re-derived from the platform's real write model, NOT
trusted from the label. An Akeneo product / product model is ONE record whose `values`
object is keyed by attribute code; each value is an ARRAY of {locale, scope, data} triples
(digest §2). Localizing locale X = adding X's triple to each translatable attribute of that
one record. There is NO per-locale sub-resource to 404-probe (that is T2) and NO
document-per-locale we track ourselves (that is T3): the source system owns the whole
correlation inside the single record. That is T1.

WHY A BESPOKE STRATEGY (shape, not the stock class). The stock FieldMapStrategy models a
T1 SUB-SHAPE — a single locale-keyed `translated_content` map written by a
read-current -> merge one locale block -> PUT the WHOLE map back. Akeneo's real semantics
are BOTH a different shape (values keyed by attribute, each a locale x scope grid, NOT one
locale map) AND better than that stock class assumes: an Akeneo product PATCH is an
officially-documented PARTIAL MERGE at the (attribute, locale, scope) TRIPLE level
(api.akeneo.com/documentation/update.html — "If the value is an object, it will be merged
with the old value"). So the write is a SURGICAL PATCH of a MINIMAL values dict with
**zero read-before-write** — we never fetch the current product to construct the write body
(digest §6.1, the single design fact this port consciously replicates). The bb-io reference
already exploits this (its UploadContent builds the body purely from the uploaded file, no
GetProductContent precedes the write). Forcing this onto the stock FieldMapStrategy's
whole-map-PUT seam would be wrong, so this connector supplies AkeneoProductStrategy, a
LocaleMappingStrategy subclass tailored to the partial-PATCH write. This is the 4th
framework-era hand-build to need a bespoke strategy (reinforces inventory §7c-2: the
topology label predicts the write SHAPE, not a reusable implementation).

Every OTHER shared primitive is reused unchanged: the flow engine (WriteBudget, egress
guard, refusal/NEEDS_REVIEW write-back exclusion, content-hash suppression), the shared D1
record lock (defence in depth only — see the strategy), base._request retry/backoff (the
reference has NONE — digest §3), the base._validate_target SSRF guard, the SelectionTracker
skip bookkeeping, and the harvest seam.

EXISTENCE PROBE / STEADY STATE (defect taxonomy C). Akeneo Community Edition has NO
draft/publish split — a PATCH lands live and a subsequent GET of the same record returns
exactly what was written (digest §2, CONFIRMED). So read, existence-probe and write all
target the SAME state: after we write locale L, the next run's GET sees L present, the probe
suppresses it, and there is no re-translate loop. (EE/Serenity Workflow-Proposals is the one
unverified risk this design cannot close hermetically — RUNBOOK MANDATORY-2.)

HELD-BACK / NO-OP (defect taxonomy B/F). A withheld (NEEDS_REVIEW / refusal) translation is
OMITTED from the outgoing values dict ENTIRELY — never a source/placeholder fallback over an
existing human translation. Akeneo's partial-PATCH merge makes this trivially safe: an
attribute absent from the write body keeps its stored value server-side. When EVERY
translatable attribute is held back the write body is empty and the strategy writes NOTHING
and reports held-back (ok=False), so the flow records NO suppression hash and a later healthy
run re-attempts (taxonomy F).

FIELD SELECTION (defect taxonomy D) — SCHEMA-DRIVEN via the /attributes endpoint (the token
can read it, unlike Strapi's content-token limit). An attribute is a translation candidate
iff its schema `type` is pim_catalog_text / pim_catalog_textarea AND `localizable` is true.
That cleanly excludes identifiers, metrics, prices, booleans, media (image/file), dates,
numbers, and select VALUES (simple/multi-select) — with a per-attribute reason REPORTED via
the shared SelectionTracker (the reference reports NONE). Select OPTION-LABEL localization
and category labels are a real but DEFERRED surface (BUILD-TODO / runbook), not built in v1.

AUTH — a pasted Bearer access token (v1 simplification of Akeneo's Connect-Apps OAuth flow;
nothing branches on how the token was obtained — digest §3). SCOPE v1 — products (uuid-keyed,
/products-uuid) + product models (code-keyed, /product-models). CHANGE DETECTION — poll +
per-(record, locale) content-hash suppression (no webhook receiver in scope for v1).

PROVENANCE: endpoint shapes are reference-and-reimplemented from bb-io/Akeneo (copyright
2023 Blackbird, MIT — verified verbatim at build time). See
THIRD_PARTY_NOTICES.md.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.services.connectors import (
    flow,
    harvest,
    webhooks,
)
from app.services.connectors.akeneo_part1 import (
    _API_PREFIX,
    _CONTENT_TYPES,
    _ENDPOINT,
    _HELD_BACK,
    _HELD_BACK_PREFIX,
    _ID_FIELD,
    _TRANSLATABLE_TYPES,
    API_VERSION,
    SPEC,
    AkeneoConnector,
    AkeneoProductStrategy,
    _akeneo_build_message,
    _akeneo_build_payload,
    _akeneo_build_result,
    _akeneo_compute_ok,
    _akeneo_is_suppressed,
    _akeneo_partition,
    _akeneo_preflight,
    _akeneo_prepare,
    _dropped_as_source,
    _held_back,
    _is_held_back,
    _parse_names,
    _PatchOutcome,
    _real_failures,
    _Unit,
    _validate_segment,
    log,
)
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
from app.services.connectors.base import (
    content_hash as _content_hash,
)
from app.services.connectors.contract import (
    AuthKind,
    Capability,
    ChangeSignals,
    ConnectorPlugin,
    RichText,
    Topology,
)
from app.services.connectors.strategy import (
    ABSENT,
    LocaleMappingStrategy,
    WriteResult,
    _record_write_lock_for,
)
from app.services.connectors.tracking import SelectionTracker

__all__ = [
    "ABSENT",
    "API_VERSION",
    "SPEC",
    "_API_PREFIX",
    "_CONTENT_TYPES",
    "_ENDPOINT",
    "_HELD_BACK",
    "_HELD_BACK_PREFIX",
    "_ID_FIELD",
    "_TRANSLATABLE_TYPES",
    "AkeneoConnector",
    "AkeneoProductStrategy",
    "AuthKind",
    "Capability",
    "ChangeSignals",
    "Connector",
    "ConnectorError",
    "ConnectorPlugin",
    "ConnectorSpec",
    "FieldSpec",
    "LocaleMappingStrategy",
    "PulledItem",
    "RichText",
    "SelectionTracker",
    "Topology",
    "WriteResult",
    "_PatchOutcome",
    "_Unit",
    "_akeneo_build_message",
    "_akeneo_build_payload",
    "_akeneo_build_result",
    "_akeneo_compute_ok",
    "_akeneo_is_suppressed",
    "_akeneo_partition",
    "_akeneo_preflight",
    "_akeneo_prepare",
    "_content_hash",
    "_dropped_as_source",
    "_held_back",
    "_is_held_back",
    "_parse_names",
    "_real_failures",
    "_record_write_lock_for",
    "_validate_segment",
    "_validate_target",
    "dataclass",
    "harvest",
    "harvest_context_field",
    "httpx",
    "log",
    "logging",
    "quote",
    "to_int",
    "webhooks",
]


def akeneo_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Akeneo product / product-model attribute values into one or more locales,
    each written back as a per-locale attribute value on the SAME record via the T1
    AkeneoProductStrategy (a surgical partial PATCH merge; other locales, scopes and untouched
    attributes are never overwritten). Thin wrapper over flow.run_translate_flow; poll-only
    with content-hash suppression + empty-record skip. only_pairs restricts the write loop to
    exactly those ('<contentType>:<uuidOrCode>', locale) pairs."""
    spec = flow.TranslateFlowSpec(
        kind="akeneo",
        no_conn_msg="No such Akeneo connection.",
        hash_key="akeneo_hashes",
        probe_label="Akeneo",
        preflight=_akeneo_preflight,
        prepare=_akeneo_prepare,
        is_suppressed=_akeneo_is_suppressed,
        build_payload=_akeneo_build_payload,
        compute_draft=lambda cfg: True,
        compute_ok=_akeneo_compute_ok,
        build_message=_akeneo_build_message,
        build_result=_akeneo_build_result,
        held_prefix=_HELD_BACK_PREFIX,
        report_held_updates=False,
        extra_counters=("empty_skipped",),
        partition=_akeneo_partition,
    )
    return flow.run_translate_flow(
        spec,
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )
