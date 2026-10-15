"""Shopify (e-commerce storefront) connector — a T2-flavored consumer of the
connector framework. Reference: bb-io/Shopify (MIT).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, API_VERSION, _SUPPORTED_RESOURCE_TYPES,
_SUPPORTED_SET, _GID_TYPE_TO_ENUM, _TRANSLATABLE_CONTENT_TYPES, _NAME_SKIP_EXACT, _STORE_HANDLE_RE,
_GID_RE, _MAX_DIGEST_RETRIES, _MAX_TOTAL_DIGEST_WAIT, _MAX_POISON_DROPS, _MAX_THROTTLE_RETRIES,
_MAX_TOTAL_THROTTLE_WAIT, _MAX_RUN_THROTTLE_WAIT, _HELD_BACK_PREFIX, _HELD_BACK,
_validate_store_handle, _Unit, _parse_names, _RegisterOutcome, SPEC, ShopifyTranslationStrategy,
_shopify_preflight, _shopify_partition, _shopify_prepare, _shopify_is_suppressed,
_shopify_on_write_ok, _shopify_build_payload, _is_held_back, _dropped_as_source,
_confirmed_incomplete, ShopifyConnector from app.services.connectors.shopify_part1,
app.services.connectors.shopify_part2.

TOPOLOGY — T2-flavored with one real deviation, re-derived (not trusted) from the
digest §7. A Shopify translation has NO independent identity: it is addressed only by
the triple (resourceId, key, locale). It is READ by querying the MASTER resource
(`translatableResource(resourceId)` -> `translatableContent {key,value,digest,type}`
for the SOURCE + `translations(locale:) {key,value}` for the registered overlay) and
WRITTEN via the single UPSERT mutation `translationsRegister` — there is no separate
create-vs-update verb and no per-locale sub-resource URL to 404-probe. That is T2 in
spirit (the SOURCE SYSTEM owns the (resource, key, locale) correlation — the connector
never invents or persists an id map, which is what separates T2 from T3) but with the
"sub-resource" flattened into a field-tuple. So Shopify supplies a SubResource STRATEGY
VARIANT (`ShopifyTranslationStrategy`, topology T2) whose:
  * existence probe reads the server-computed `translations(locale:)` overlay (a real
    key-presence signal), NOT an HTTP GET -> 404 probe (there is no such URL); and
  * write ALWAYS carries a freshly-read `translatableContentDigest` per key (§3) —
    Shopify rejects a `translationsRegister` whose digest does not match the CURRENT
    source content (`INVALID_TRANSLATABLE_CONTENT`), so the digest is the load-bearing write gate.

All access is the Admin GraphQL API, one endpoint per store:
`https://{store}.myshopify.com/admin/api/{API_VERSION}/graphql.json`. API_VERSION is a
CURRENT stable version pinned below (the reference pinned a stale `2024-04` — NOT
ported; re-verify the pin is still supported at build/deploy time — see the runbook).

THE THREE DIGEST GOTCHAS ARE DESIGN REQUIREMENTS (digest §6):
  1. store_name HOST INJECTION (the reference's own defect — it only checks the value
     is not an absolute URI). We ALLOWLIST the store handle to Shopify's own charset
     (`[a-z0-9]` with single interior hyphens, no leading/trailing hyphen) BEFORE it is
     ever interpolated into the API URL, fail-closed (taxonomy A). A `/`, `.`, `@`,
     unicode char, whitespace or empty handle is refused, so the Admin token can never
     be sent to an attacker-controlled host. `<handle>.myshopify.com` is accepted and
     reduced to its handle.
  2. DIGEST FRESHNESS + PER-KEY SOURCE-DRIFT DROP. We RE-READ the source digest
     immediately before EVERY `translationsRegister` (the reference carries stale
     download-time digests for first-time translations — NOT ported), so an UNCHANGED
     source never false-rejects. But a fresh digest ALONE would LAUNDER a stale translation
     past Shopify's own staleness gate — the digest exists to prove "you translated from
     the CURRENT content". So each write also carries, per key, the SOURCE TEXT the
     translation was made from, and before registering we compare it to the freshly-read
     source PER KEY: a key whose source changed is DROPPED (recorded incomplete, "source
     changed during translation") and the UN-drifted keys still register — translationsRegister
     carries a per-key digest, so there is no cross-key atomicity to preserve and one
     flapping field (price/inventory/timestamp) can't livelock the whole resource. The
     dropped key returns AUTOMATICALLY next run (its source change alters the content hash).
     If EVERY key drifted, nothing coherent remains and the pair aborts (retryable, no
     hash). A residual digest-mismatch userError from a race between the drift check and the
     register is retried (bounded, on its own budget) and converges to the same per-key drop.
  3. NO SILENT NO-OP SKIP. The reference silently skips registering any translation
     whose value equals the SOURCE (a SKU/brand/number) — so that key looks "never
     translated" forever and is re-picked every run (the Strapi N3 perpetual-retry
     bug). We do NOT port that filter: a legitimately-identical translation is
     REGISTERED like any other, so the existence probe finds it and the second run is
     correctly suppressed (steady state, not a re-pick).

RATE LIMITING (digest §5). The Admin GraphQL API uses cost-based leaky-bucket
throttling: a throttled query returns HTTP 200 with a top-level `errors` entry whose
`extensions.code == "THROTTLED"` plus `extensions.cost.throttleStatus`. The reference
implements NO retry at all. We honor the throttle status: bounded backoff computed from
`throttleStatus` (or exponential fallback), each wait clamped at base.MAX_SLEEP and the
per-call total clamped, then retry the (idempotent upsert / read) query.

FIELD SELECTION — SERVER-DRIVEN. Unlike Strapi/Contentful there is no client-side
field-classification heuristic on the READ side: Shopify's `translatableContent` list
for a resource IS the allowlist of translatable keys (digest §3). We additionally gate
on the content `type` (translate STRING/HTML; skip + REPORT machine types — JSON, URI,
LINK, RICH_TEXT/`*_REFERENCE`, LIST_* — which must not be MT'd as plain text) and skip
id/slug keys (`handle`, the URL slug) by name in AUTO mode. An operator `fields`
allowlist / `exclude_fields` overrides. Everything skipped is reported.

SOURCE LOCALE — Shopify's translatableContent is ALWAYS the shop's PRIMARY locale (§3),
so the source we translate FROM is the primary, full stop. The `source_locale` config is
informational and MUST match the primary — preflight fails loudly if it does not — and the
primary is UNCONDITIONALLY dropped from the target set so MT can never be registered ONTO
the primary (HIGH-3).

WRITE MODEL — narrow upsert. `translationsRegister` registers ONLY the keys we actually
translated this run (a held-back NEEDS_REVIEW key is omitted -> its stored translation
survives; taxonomy B), each carrying a FRESH source digest. A poison key (a non-digest
userError Shopify attributes to it) and a DRIFTED key (its source changed since
translation) are DROPPED per key and the good remainder is still registered (MEDIUM-1 /
HIGH-2). Recovery differs by cause: an all-NEEDS_REVIEW pair writes NOTHING and records NO
hash, so it IS re-attempted next run (taxonomy F); a HELD or POISON-dropped key on a
PARTIAL write is NEVER auto-re-attempted (it was never registered, so the full-coverage
probe treats the pair as covered) — its recovery is operator-only, via retry (which clears
the pair's hash) OR a source edit (which changes the content hash); a DRIFTED key returns
AUTOMATICALLY next run because its source change alters the content hash. Every dropped key
is surfaced as an operator-visible INCOMPLETE marker.

SUPPRESSION — poll + per-(resource, locale) content-hash AND FULL-coverage: a pair is
suppressed only when the hash matches AND every key we previously REGISTERED for it is
still present in the live `translations(locale:)` overlay (MEDIUM-3). A bare any-key probe
would leave a single externally-DELETED key's translation unrestored forever; keying on the
registered set surfaces a deleted key for restore while NOT forcing a re-attempt of a
held/poison key we never registered (so a partial pair is SUPPRESSED, not re-spent, until an
operator retry or a source edit). No watermark (`updatedAt` is metadata-tainted; the server
`outdated` flag is a documented later refinement).

DISCOVERY — cursor-paginated per resource type, filled round-robin across types. Like
Contentful, the shared cap is fetched per type before the round-robin merge, so a run with
T configured types issues up to T x (cap/pagesize) list calls — an accepted small
amplification (LOW-4), bounded by max_entries and the per-run throttle budget.

DEFERRED FROM v1 (studied, deliberately not half-built — see the runbook + report):
client-credentials 24h-token auth (v1 = Admin API access token only), the Metafield /
product-option / theme / shop / menu / policy resource types (v1 = PRODUCT / COLLECTION
/ ARTICLE / BLOG / PAGE), market-scoped (`marketId`) translations, HTML-aware
segmentation of `body_html` (v1 translates each field value whole, like Strapi), and
WEBHOOKS (poll-first precedent — Shopify webhooks need HMAC verification against a real
payload this build cannot exercise offline; change detection is poll + content-hash).

PROVENANCE: endpoint shapes are reference-and-reimplemented from bb-io/Shopify
(© 2023 Blackbird, MIT — verified verbatim at build time). See
THIRD_PARTY_NOTICES.md.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
import re

from app.services.connectors import base, flow, harvest, webhooks
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
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
from app.services.connectors.shopify_part1 import (
    _GID_RE,
    _GID_TYPE_TO_ENUM,
    _HELD_BACK,
    _HELD_BACK_PREFIX,
    _MAX_DIGEST_RETRIES,
    _MAX_POISON_DROPS,
    _MAX_RUN_THROTTLE_WAIT,
    _MAX_THROTTLE_RETRIES,
    _MAX_TOTAL_DIGEST_WAIT,
    _MAX_TOTAL_THROTTLE_WAIT,
    _NAME_SKIP_EXACT,
    _STORE_HANDLE_RE,
    _SUPPORTED_RESOURCE_TYPES,
    _SUPPORTED_SET,
    _TRANSLATABLE_CONTENT_TYPES,
    API_VERSION,
    SPEC,
    ShopifyTranslationStrategy,
    _confirmed_incomplete,
    _dropped_as_source,
    _is_held_back,
    _parse_names,
    _RegisterOutcome,
    _shopify_build_payload,
    _shopify_is_suppressed,
    _shopify_on_write_ok,
    _shopify_partition,
    _shopify_preflight,
    _shopify_prepare,
    _Unit,
    _validate_store_handle,
    log,
)
from app.services.connectors.shopify_part2 import ShopifyConnector
from app.services.connectors.strategy import (
    ABSENT,
    LocaleMappingStrategy,
    WriteResult,
    _write_lock_for,
)

__all__ = [
    "ABSENT",
    "API_VERSION",
    "SPEC",
    "_GID_RE",
    "_GID_TYPE_TO_ENUM",
    "_HELD_BACK",
    "_HELD_BACK_PREFIX",
    "_MAX_DIGEST_RETRIES",
    "_MAX_POISON_DROPS",
    "_MAX_RUN_THROTTLE_WAIT",
    "_MAX_THROTTLE_RETRIES",
    "_MAX_TOTAL_DIGEST_WAIT",
    "_MAX_TOTAL_THROTTLE_WAIT",
    "_NAME_SKIP_EXACT",
    "_STORE_HANDLE_RE",
    "_SUPPORTED_RESOURCE_TYPES",
    "_SUPPORTED_SET",
    "_TRANSLATABLE_CONTENT_TYPES",
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
    "ShopifyConnector",
    "ShopifyTranslationStrategy",
    "Topology",
    "WriteResult",
    "_RegisterOutcome",
    "_Unit",
    "_confirmed_incomplete",
    "_content_hash",
    "_dropped_as_source",
    "_is_held_back",
    "_parse_names",
    "_shopify_build_payload",
    "_shopify_is_suppressed",
    "_shopify_on_write_ok",
    "_shopify_partition",
    "_shopify_preflight",
    "_shopify_prepare",
    "_validate_store_handle",
    "_write_lock_for",
    "base",
    "harvest",
    "harvest_context_field",
    "log",
    "logging",
    "re",
    "to_int",
    "webhooks",
]


def _held_back(ctx):
    return [f for f in ctx.failures if _is_held_back(f)]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_held_back(f)]


def _shopify_compute_ok(ctx):
    if not ctx.usable:
        return False
    real = _real_failures(ctx)
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not real)
        or (not real and (ctx.created + ctx.updated + ctx.skipped) == 0)
    )


def _shopify_build_message(ctx):
    parts = [
        f"Registered {ctx.created} new, updated {ctx.updated} translation set(s) "
        f"across {len(ctx.usable)} locale(s) from {len(ctx.articles)} resource(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) locale(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} resource(s) with no translatable fields."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} locale(s) not on this Shopify store: "
            f"{', '.join(ctx.rejected)}."
        )
    dropped = _dropped_as_source(ctx)
    if dropped:
        parts.append(
            f"Skipped {len(dropped)} target(s) equal to the source locale "
            f"({', '.join(dropped)}) - configure different target locales."
        )
    held = _held_back(ctx)
    if held:
        parts.append(
            f"Held back {len(held)} (resource, locale) pair(s) awaiting review "
            f"(NEEDS_REVIEW) - nothing written; they will be re-attempted next run."
        )
    incomplete = _confirmed_incomplete(ctx)
    if incomplete:
        sample = "; ".join(
            f"{p['article_id']}/{p['locale']}: {', '.join(p['held_fields'])}"
            for p in incomplete[:3]
        )
        parts.append(
            f"Wrote {len(incomplete)} pair(s) INCOMPLETE - some key(s) held back "
            f"(NEEDS_REVIEW / rejected / source-changed), the rest were registered: "
            f"{sample}. A held/rejected key needs an explicit RETRY (a plain re-run "
            f"skips it - the pair is covered); a source-changed key re-attempts on "
            f"its own next run."
        )
    # Operator-visible unpublished-locale note (digest §6.6): a write to an enabled-but-
    # unpublished locale succeeds but is invisible on the storefront until published.
    try:
        unpub = ctx.connector.unpublished_locales() & set(ctx.usable)
    except Exception:
        unpub = set()
    if unpub:
        parts.append(
            f"Note: locale(s) {', '.join(sorted(unpub))} are added but NOT "
            f"published in Shopify - the translations were registered but will "
            f"not show on the storefront until you publish the language "
            f"(Settings -> Languages)."
        )
    sel = ctx.connector._selection_summary()
    if sel:
        parts.append(sel)
    real = _real_failures(ctx)
    if real:
        parts.append(
            "Errors: "
            + "; ".join(
                f"{f.get('article_id')}/{f.get('locale')}: {f.get('error')}" for f in real[:3]
            )
        )
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("resource write(s)").strip())
        log.warning(
            "shopify_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _shopify_variant_exists(connector, aid, loc):
    """#640: does the shop already hold ANY registered translation for this (resource,
    locale)? A first write with a unit held back would leave that field showing the
    SOURCE-language text on the storefront (Shopify falls back to the primary language for
    an unregistered key), so the flow holds the whole pair instead. Not exists_target: that
    means "every key we tracked is still covered" and is False for an untouched locale."""
    exact = connector.resolve_locale(loc)
    return bool(connector.existing_translation_keys(aid, exact))


def _shopify_build_result(ctx):
    # NOTE: the registered-key set is persisted per-pair in _shopify_on_write_ok (BEFORE
    # the hash merge), not here (H1) — build_result runs AFTER the hash, which would leave
    # a crash window of hash-without-tracking.
    return {
        "resources": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "held_back": len(_held_back(ctx)),
        "empty_skipped": ctx.counters["empty_skipped"],
        "rejected": ctx.rejected,
        "dropped_source": _dropped_as_source(ctx),
        "incomplete": _confirmed_incomplete(ctx),
        "fields_selected": sorted(ctx.connector._sel_selected),
        "fields_skipped": dict(ctx.connector._sel_skipped),
        "failures": _real_failures(ctx),
    }


def shopify_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate Shopify resources into one or more locales, each REGISTERED against the
    source resource via the T2-flavored ShopifyTranslationStrategy (translationsRegister,
    addressed by resource + key + locale, each carrying a freshly-read source digest — so
    the source content is never overwritten; a key whose source changed since translation is
    dropped per key, never registered stale). Thin wrapper over flow.run_translate_flow;
    poll-only with content-hash + FULL-coverage suppression (every registered key must still
    be present) + empty-resource skip. A partial write is surfaced as an operator-visible
    incomplete pair; a held/poison-dropped key is NOT auto-re-attempted (the pair is covered)
    — recovery is retry_failed (which passes the pair as only_pairs, and the flow bypasses
    suppression for it) or a source edit; a drifted key auto-returns via its source change.
    only_pairs restricts the write loop to exactly those (resource_id, locale) pairs and
    bypasses content-hash suppression for them (an explicit operator retry)."""
    spec = flow.TranslateFlowSpec(
        kind="shopify",
        no_conn_msg="No such Shopify connection.",
        hash_key="shopify_hashes",
        probe_label="Shopify",
        preflight=_shopify_preflight,
        prepare=_shopify_prepare,
        is_suppressed=_shopify_is_suppressed,
        build_payload=_shopify_build_payload,
        compute_draft=lambda cfg: True,
        compute_ok=_shopify_compute_ok,
        build_message=_shopify_build_message,
        build_result=_shopify_build_result,
        variant_exists=_shopify_variant_exists,
        on_write_ok=_shopify_on_write_ok,
        extra_counters=("empty_skipped",),
        partition=_shopify_partition,
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
