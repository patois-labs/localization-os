"""Strapi (Headless CMS) connector — a T2 consumer of the connector framework.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, API_VERSION, _SUPPORTED_VERSIONS,
_SYSTEM_FIELDS, _RESERVED_WRITE_KEYS, _ALIAS_PREFIX, _TITLE_FALLBACK_KEY, _HELD_BACK_PREFIX,
_HELD_BACK, _NAME_SKIP_EXACT, _NAME_ID_TOKENS, _WORD_RE, _CAMEL_ID_RE, _EMAIL_RE, _URL_RE,
_DATETIME_RE, _TIME_RE, _name_words, _name_looks_id, _classify_field, _Unit, _parse_names, _stash,
_strapi_preflight, _strapi_partition, _strapi_prepare, _strapi_is_suppressed, _strapi_build_payload,
_dropped_as_source, _is_held_back, _held_back, _real_failures, SPEC, StrapiConnector from
app.services.connectors.strapi_field_classification_heu, app.services.connectors.strapi_part2.

TOPOLOGY — T2 (entry/translation-per-locale). A Strapi document has ONE master
identity — `documentId` (v5) or the numeric master `id` + a `localizations` link
(v4) — and each locale is its OWN entry variant addressed by `?locale=<code>`. The
SOURCE SYSTEM owns the correlation, which is what separates T2 from T3, so write-back
rides the shared `strategy.SubResourceStrategy` (404-vs-200 existence probe -> create
or update).

WRITE MODEL — narrow, partial-PUT (adversarial review M1). We write back ONLY the
fields we actually translated this run; Strapi's PUT is already a partial merge, so
every field we do NOT send keeps its stored value. That is what protects an existing
human/target translation from being clobbered — NOT a read-current-merge (a
read-current-merge would re-send our stale read of every field, converting a
same-field race into an all-fields race, and would drag relations/media into the
write body). Relations, media, components and every non-text field are therefore
never in the write body. Consequence (documented in the connection UI): concurrent
writers of the SAME field on the SAME (document, locale) are last-write-wins across
our GET-probe / PUT window — acceptable at WORKER_COUNT 2; the D1 lock serializes
in-process writers.

HELD-BACK FIELDS (review H2 — the K1-class "guard at the write seam for EVERY field"
lesson). A NEEDS_REVIEW / refusal-held title or body unit is OMITTED from the write
body, so an existing stored translation for it survives (partial PUT). Issue #640: a
brand-new locale variant is NOT created while ANY of its fields is held (the flow engine
checks this before the write, spec.held_prefix), because a variant created with a field
missing stays that way; and a pair with a held field never records its content hash, so
the next run retries it once the segment is approved. Body fields are never
source-seeded (never write source as a stand-in translation).

DRAFT & PUBLISH (review H4). v5 REST reads default to the PUBLISHED version while our
writes land on the DRAFT — a mismatch that makes the existence probe 404 forever and
re-translate every run. So every v5 READ/PROBE passes `status=draft`, targeting the
same version the write does. We never publish (draft-only on v5). v4 has NO document
draft: a v4 PUT updates the LIVE entry directly, so "draft-only" does not apply there
and `compute_draft` is inert.

FIELD DISCOVERY (review H3, tightened per N2). Strapi's content-type SCHEMA is not
reachable with a content API token — the Content-Type Builder endpoint requires an
admin JWT — so we cannot introspect field types. Auto-discovery therefore uses
CONSERVATIVE HEURISTICS that skip a field only when (a) its NAME is an id/slug marker
— an exact match like `slug`/`status`/`email`, or `slug`/`uid` as a whole WORD inside a
compound (`metaSlug`, `productUid`), or an `id`/`*_id`/`…Id` name — or (b) its VALUE is
an unambiguous machine format (email / url / ISO date-time). It deliberately does NOT
skip on value SHAPE alone: hyphenated UI copy (`sign-up`) or a long lowercase word
(`internationalization`) is ordinary translatable text and is SELECTED. It fails safe
(an un-caught non-text field round-trips its string harmlessly) and reports every
selected + skipped field, with the reason, in the run message + result. `blocks`
(v5's default rich-text type), `json` and `component` fields are arrays/objects —
skipped with a LOUD reason rather than silently, and per-format AST segmentation for
them is a documented residual for a later build. An operator `fields` allowlist
overrides the heuristics entirely (the recommended path for a known schema).

VERSIONS — v5 primary, v4 supported behind `strapi_version`. CHANGE DETECTION —
polling + content-hash suppression (Strapi webhooks are unsigned; not wired).

PROVENANCE: endpoint shapes are reference-and-reimplemented from bb-io/Strapi
(© 2023 Blackbird, MIT). See THIRD_PARTY_NOTICES.md.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx

from app.services.connectors import (
    flow,
    harvest,
    webhooks,
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
from app.services.connectors.strapi_field_classification_heu import (
    _ALIAS_PREFIX,
    _CAMEL_ID_RE,
    _DATETIME_RE,
    _EMAIL_RE,
    _HELD_BACK,
    _HELD_BACK_PREFIX,
    _NAME_ID_TOKENS,
    _NAME_SKIP_EXACT,
    _RESERVED_WRITE_KEYS,
    _SUPPORTED_VERSIONS,
    _SYSTEM_FIELDS,
    _TIME_RE,
    _TITLE_FALLBACK_KEY,
    _URL_RE,
    _WORD_RE,
    API_VERSION,
    _classify_field,
    _dropped_as_source,
    _held_back,
    _is_held_back,
    _name_looks_id,
    _name_words,
    _parse_names,
    _real_failures,
    _stash,
    _strapi_build_payload,
    _strapi_is_suppressed,
    _strapi_partition,
    _strapi_preflight,
    _strapi_prepare,
    _Unit,
    log,
)
from app.services.connectors.strapi_part2 import SPEC, StrapiConnector
from app.services.connectors.strategy import ABSENT, SubResourceStrategy, WriteResult

__all__ = [
    "ABSENT",
    "API_VERSION",
    "SPEC",
    "_ALIAS_PREFIX",
    "_CAMEL_ID_RE",
    "_DATETIME_RE",
    "_EMAIL_RE",
    "_HELD_BACK",
    "_HELD_BACK_PREFIX",
    "_NAME_ID_TOKENS",
    "_NAME_SKIP_EXACT",
    "_RESERVED_WRITE_KEYS",
    "_SUPPORTED_VERSIONS",
    "_SYSTEM_FIELDS",
    "_TIME_RE",
    "_TITLE_FALLBACK_KEY",
    "_URL_RE",
    "_WORD_RE",
    "AuthKind",
    "Capability",
    "ChangeSignals",
    "Connector",
    "ConnectorError",
    "ConnectorPlugin",
    "ConnectorSpec",
    "FieldSpec",
    "PulledItem",
    "RichText",
    "StrapiConnector",
    "SubResourceStrategy",
    "Topology",
    "WriteResult",
    "_Unit",
    "_classify_field",
    "_content_hash",
    "_dropped_as_source",
    "_held_back",
    "_is_held_back",
    "_name_looks_id",
    "_name_words",
    "_parse_names",
    "_real_failures",
    "_stash",
    "_strapi_build_payload",
    "_strapi_is_suppressed",
    "_strapi_partition",
    "_strapi_preflight",
    "_strapi_prepare",
    "_validate_target",
    "harvest",
    "harvest_context_field",
    "httpx",
    "log",
    "logging",
    "quote",
    "re",
    "to_int",
    "webhooks",
]


def _strapi_compute_ok(ctx):
    if not ctx.usable:
        return False  # nothing translatable (all rejected and/or all source-equal)
    real = _real_failures(ctx)
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not real)
        or (not real and (ctx.created + ctx.updated + ctx.skipped) == 0)
    )


def _strapi_build_message(ctx):
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} entry variant(s) across "
        f"{len(ctx.usable)} locale(s) from {len(ctx.articles)} entry(ies)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) locale(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} entry(ies) with no translatable fields."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} locale(s) not enabled in Strapi: "
            f"{', '.join(ctx.rejected)}."
        )
    dropped = _dropped_as_source(ctx)
    if dropped:
        parts.append(
            f"Skipped {len(dropped)} target(s) equal to the source locale "
            f"({', '.join(dropped)}) — configure different target locales."
        )
    held = _held_back(ctx)
    if held:
        parts.append(
            f"Held back {len(held)} (entry, locale) pair(s) awaiting review "
            f"(NEEDS_REVIEW): a new locale variant is not created while any of its fields "
            f"is held, and an existing one keeps the held field's stored value. "
            f"They will be re-attempted next run."
        )
    sel = ctx.connector._selection_summary()
    if sel:
        parts.append(sel)
    # Only REAL write failures are surfaced as errors; held-back pairs are reported above.
    # Built from _real_failures (a PREFIX classification, consistent with _is_held_back) so
    # a genuine error whose BODY merely CONTAINS "held-back:" is never dropped from display.
    real = _real_failures(ctx)
    if real:
        parts.append(
            "Errors: "
            + "; ".join(
                f"{f.get('article_id')}/{f.get('locale')}: {f.get('error')}" for f in real[:3]
            )
        )
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("entry write(s)").strip())
        log.warning(
            "strapi_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _strapi_build_result(ctx):
    # `failures` reports only REAL write failures (retryable); held-back pairs are their
    # own counter and are deliberately NOT in `failures` (retrying MT on a deterministic
    # NEEDS_REVIEW hold-back is pointless — the no-hash guarantees a natural re-attempt).
    return {
        "entries": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "held_back": len(_held_back(ctx)),
        "empty_skipped": ctx.counters["empty_skipped"],
        "rejected": ctx.rejected,
        "dropped_source": _dropped_as_source(ctx),
        "fields_selected": sorted(ctx.connector._sel_selected),
        "fields_skipped": dict(ctx.connector._sel_skipped),
        "failures": _real_failures(ctx),
    }


def strapi_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Strapi collection-type entries into one or more locales, each written
    back as the entry's per-locale variant via the T2 SubResourceStrategy (v5 keyed by
    documentId + status=draft, v4 by localization). Thin wrapper over
    flow.run_translate_flow; poll-only with content-hash suppression + empty-entry skip.
    only_pairs restricts the write loop to exactly those ('<ct>:<id>', locale) pairs."""
    spec = flow.TranslateFlowSpec(
        kind="strapi",
        no_conn_msg="No such Strapi connection.",
        hash_key="strapi_hashes",
        probe_label="Strapi",
        preflight=_strapi_preflight,
        prepare=_strapi_prepare,
        is_suppressed=_strapi_is_suppressed,
        build_payload=_strapi_build_payload,
        compute_draft=lambda cfg: True,
        compute_ok=_strapi_compute_ok,
        build_message=_strapi_build_message,
        build_result=_strapi_build_result,
        extra_counters=("empty_skipped",),
        partition=_strapi_partition,
        held_prefix=_HELD_BACK_PREFIX,
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
