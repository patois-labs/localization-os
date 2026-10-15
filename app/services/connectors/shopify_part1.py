"""shopify: part1

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
import re

from app.services.connectors import base, flow
from app.services.connectors.base import (
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    harvest_context_field,
)
from app.services.connectors.strategy import (
    ABSENT,
    LocaleMappingStrategy,
    WriteResult,
    _write_lock_for,
)

log = logging.getLogger("loc_tms.connectors")


API_VERSION = "2026-10"


_SUPPORTED_RESOURCE_TYPES = ("PRODUCT", "COLLECTION", "ARTICLE", "BLOG", "PAGE")


_SUPPORTED_SET = frozenset(_SUPPORTED_RESOURCE_TYPES)


_GID_TYPE_TO_ENUM = {
    "Product": "PRODUCT",
    "Collection": "COLLECTION",
    "Article": "ARTICLE",
    "OnlineStoreArticle": "ARTICLE",
    "Blog": "BLOG",
    "OnlineStoreBlog": "BLOG",
    "Page": "PAGE",
    "OnlineStorePage": "PAGE",
}


_TRANSLATABLE_CONTENT_TYPES = frozenset({"STRING", "HTML"})


_NAME_SKIP_EXACT = frozenset({"handle"})


_STORE_HANDLE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


_GID_RE = re.compile(r"^gid://shopify/([A-Za-z]+)/([0-9]{1,20})$")


_MAX_DIGEST_RETRIES = 4


_MAX_TOTAL_DIGEST_WAIT = 60.0


_MAX_POISON_DROPS = 25


_MAX_THROTTLE_RETRIES = 5


_MAX_TOTAL_THROTTLE_WAIT = 60.0


_MAX_RUN_THROTTLE_WAIT = 180.0


# translationsRegister userError codes that mean "the digest you sent no longer matches the
# source". Shopify's TranslationErrorCode enum (shopify.dev 2026-10) has NO digest-specific
# code (the invented INVALID_DIGEST is not in it); the closest is INVALID_TRANSLATABLE_CONTENT.
# Treating it as a digest race is safe: the retry re-reads the source and re-checks drift on a
# bounded budget, then fails the pair loudly and retryably. The live QA run must confirm the
# code (runbook MANDATORY-1).
_DIGEST_RACE_CODES = frozenset({"INVALID_TRANSLATABLE_CONTENT"})


_HELD_BACK_PREFIX = "held-back:"


_HELD_BACK = (
    _HELD_BACK_PREFIX + " every translatable field is awaiting review "
    "(NEEDS_REVIEW); nothing written, existing translation (if any) kept"
)


# CURRENT stable Admin API version. The reference pinned a stale "2024-04"; do NOT port
# that. Shopify deprecates a version ~12 months after release, so re-verify this is
# still supported at build/deploy time (runbook MANDATORY check). Not user input — a
# module constant — so no injection concern.
# The TranslatableResource enum values this v1 supports (the generic "content" types,
# digest §1). Metafield / product options+values / theme / shop / menu / policy are
# deliberate residuals (see the module docstring). Operator config is validated against
# this set (taxonomy A — operator strings, not just per-request input).
# GID type token -> our resource-type enum, for SCOPE-checking an explicit resourceId
# (taxonomy A). Fail-closed: an unrecognised GID type is refused. The online-store
# variants are included because Shopify GIDs for these differ across surfaces; the map
# may need a live re-verify (runbook), and an unmapped type simply refuses (safe).
# Content `type`s (of a translatableContent entry) we translate as text. Everything else
# — JSON, URI, LINK, RICH_TEXT_FIELD, FILE_REFERENCE, *_REFERENCE, LIST_* — is a machine
# value that must NOT be MT'd as plain text; it is skipped + REPORTED (taxonomy D).
# Keys Shopify flags translatable that are nonetheless URL slugs / ids — skipped by NAME
# in AUTO mode (localizing a `handle` rewrites the storefront URL slug; taxonomy D). The
# `fields` allowlist overrides. Kept tiny + explicit (the Strapi/Contentful N2 lesson:
# a looser set eats real copy).
# Store-handle charset: Shopify's own myshopify subdomain rule — lowercase alphanumerics
# with single interior hyphens, no leading/trailing hyphen. This is the gotcha-#1
# allowlist; it forbids '/', '.', '@', whitespace, unicode and empty by construction.
# Numeric id capped at 20 digits (LOW-2) — a real Shopify id is a 64-bit int; the cap
# stops a pathological id from ever reaching the wire as a GraphQL variable.
# Bounded refetch-and-retry on a genuine (non-drift) INVALID_DIGEST from a race. Each
# attempt re-reads the CURRENT digest fresh; exhausted -> abort the pair (no hash), so the
# next run retries. Mirrors Contentful's version-conflict loop.
# Independent bound (M2) on the per-key poison-DROP loop, so Shopify rejecting keys one at
# a time cannot consume the digest-retry budget and fail the pair with a misleading
# "INVALID_DIGEST" tail. Each drop removes >=1 key, so this also bounds by construction.
# Bounded backoff on cost-based THROTTLED errors (digest §5); per-wait clamped at
# base.MAX_SLEEP, per-call total clamped so a hostile throttleStatus cannot stall a call,
# and a per-RUN total (LOW-1) clamped so a persistently-throttled run fails cleanly
# (retryable) instead of accumulating unbounded backoff across its many GraphQL calls.
# WriteResult.message marker for an (resource, locale) whose every translatable key was
# held back (NEEDS_REVIEW) this run: ok=False so the flow records NO suppression hash
# (taxonomy F), then accounted as `held_back` (not a real failure).
def _validate_store_handle(raw) -> str:
    """The gotcha-#1 allowlist. Return the validated lowercase store HANDLE, or raise.

    Accepts either a bare handle (`acme-eu`) or a full `acme-eu.myshopify.com`, reduces
    the latter to its handle, and requires the result to match Shopify's own subdomain
    charset. A value containing '/', '.', '@', whitespace, unicode, or empty is refused
    BEFORE it can reach URL construction — so the Admin API access token is never sent to
    an attacker-controlled host (the reference's defect, digest §2/§6.1)."""
    from app.services.connectors.tenant_host import tenant_handle

    v = tenant_handle(raw, "myshopify.com")
    if not v:
        raise ConnectorError(
            "Shopify store name is required (your store handle, e.g. "
            "'acme' for acme.myshopify.com)."
        )
    # Reduce a full myshopify host to its handle (exact suffix only — never a substring).
    if v.endswith(".myshopify.com"):
        v = v[: -len(".myshopify.com")]
    if not _STORE_HANDLE_RE.match(v):
        raise ConnectorError(
            f"Invalid Shopify store name {raw!r}: use just the store handle "
            f"(lowercase letters, numbers and interior hyphens, e.g. 'acme' or "
            f"'acme-eu'), not a URL, path or domain."
        )
    return v


class _Unit:
    """A minimal flow translation unit (the engine reads only .text + .p_index).
    p_index is the translatable KEY, so build_payload reinserts each translation into the
    exact field it came from."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index: str):
        self.text = text
        self.p_index = p_index


def _parse_names(raw) -> list[str]:
    out: list[str] = []
    for x in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        n = x.strip()
        if n and n not in out:
            out.append(n)
    return out


class _RegisterOutcome:
    """Result of one translationsRegister mutation: ok (no userErrors), or a failure
    carrying the joined userError message. invalid_digest latches when ANY userError's
    code is in _DIGEST_RACE_CODES, so the strategy re-reads a fresh digest and retries. bad_keys
    is the set of input KEYS a non-digest userError could be attributed to (via the error's
    field-path index), so the strategy can DROP just those keys and register the rest
    (MEDIUM-1); empty when nothing is attributable (-> whole-pair failure)."""

    __slots__ = ("bad_keys", "invalid_digest", "message", "ok")

    def __init__(self, ok: bool, invalid_digest: bool = False, message: str = "", bad_keys=None):
        self.ok = ok
        self.invalid_digest = invalid_digest
        self.message = message
        self.bad_keys = bad_keys or set()


SPEC = ConnectorSpec(
    kind="shopify",
    name="Shopify",
    icon="\U0001f6d2",  # shopping cart
    cat="E-commerce",
    blurb="Translate Shopify products, collections, articles, blogs and pages into any "
    "language. Each translation is registered against the source resource via the "
    "Admin API Translations API (translationsRegister, addressed by resource + key "
    "+ locale, digest-gated), so the source content is never overwritten. Locales "
    "must already exist in Shopify (Settings -> Languages); publish each to show it "
    "on the storefront.",
    config_fields=[
        FieldSpec(
            "store_name",
            "Store name",
            "Your store handle, e.g. 'acme' for "
            "acme.myshopify.com (a full '.myshopify.com' domain is accepted too). "
            "Lowercase letters, numbers and interior hyphens only.",
            placeholder="acme",
        ),
        FieldSpec(
            "resource_types",
            "Resource types",
            "Comma-separated Shopify resource "
            "types to translate: any of PRODUCT, COLLECTION, ARTICLE, BLOG, PAGE "
            "(default PRODUCT).",
            required=False,
            placeholder="PRODUCT, COLLECTION",
        ),
        FieldSpec(
            "fields",
            "Translatable keys",
            "Optional allowlist of translatable "
            "content keys to translate (comma-separated, e.g. 'title, body_html'). "
            "Blank = translate every text/HTML key Shopify marks translatable "
            "(machine-typed keys and the URL 'handle' are excluded; the run report "
            "lists selected + skipped).",
            required=False,
            placeholder="title, body_html",
        ),
        FieldSpec(
            "exclude_fields",
            "Exclude keys",
            "Content keys to NEVER translate "
            "(comma-separated). Applies only when the allowlist above is blank.",
            required=False,
            placeholder="handle, meta_title",
        ),
        FieldSpec(
            "title_field",
            "Title key",
            "Content key to use as the resource's display title (default: 'title').",
            required=False,
            placeholder="title",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "Informational only: Shopify always "
            "translates FROM the shop's PRIMARY locale. Leave blank; if set, it MUST "
            "match the primary or the run fails with the primary named.",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated Shopify locale "
            "codes to write (must already be added under Settings -> Languages).",
            required=False,
            placeholder="fr, de, es",
        ),
        FieldSpec(
            "max_entries",
            "Max resources",
            "Safety cap on resources scanned per run.",
            required=False,
            placeholder="50",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "client_id",
            "Client ID",
            "From the Dev Dashboard (dev.shopify.com), your app, Settings. Enter the "
            "client ID and client secret and the connector fetches and renews the 24-hour "
            "Admin API token itself. The app and the store must be in the same Shopify "
            "organization. Leave blank if you use an access token below.",
            required=False,
            secret=False,
            placeholder="client id",
        ),
        FieldSpec(
            "client_secret",
            "Client secret",
            "From the same Settings page as the client ID. Stored encrypted; only used to "
            "request a token from your own store.",
            required=False,
            secret=True,
            placeholder="shpss_...",
        ),
        FieldSpec(
            "access_token",
            "Admin API access token",
            "Optional alternative to the client ID and secret: a long-lived Admin API "
            "access token from a legacy custom app (older stores only), scoped for "
            "read_locales, read/write_translations and read access to the resource types "
            "you translate. Sent as the X-Shopify-Access-Token header.",
            required=False,
            secret=True,
            placeholder="shpat_...",
        ),
    ],
    setup_steps=[
        "In Shopify admin, add each target language under Settings -> Languages and "
        "PUBLISH it (an unpublished language accepts translations but they do not show on "
        "the storefront until published).",
        "In the Shopify Dev Dashboard (dev.shopify.com/dashboard) choose Apps -> Create app "
        "-> Start from Dev Dashboard. On the app's Versions page select the Admin API "
        "scopes read_locales, read_translations, write_translations, and read access to "
        "the resources you translate (read_products, read_content, "
        "read_online_store_pages), then Release the version.",
        "Choose Install app and install it on your store. The app and the store must "
        "belong to the same Shopify organization (a store created from the Dev "
        "Dashboard's Stores page does).",
        "Open the app's Settings page and copy the Client ID and Client secret.",
        "Enter the store name (your 'acme' handle), the resource types (e.g. 'PRODUCT'), "
        "the client ID and secret, and your target locales, then press Test connection. "
        "The connector requests and renews the 24-hour Admin API token itself. (Older stores "
        "can still paste a long-lived Admin API access token instead.)",
    ],
    other_side="Change detection is poll-based: run on a schedule. Shopify webhooks are "
    "out of scope for v1. Each translation is registered against the source "
    "resource (resource + key + locale) with a freshly-read content digest and "
    "never overwrites the source. Locales must already exist in Shopify; "
    "publish a language under Settings -> Languages for it to appear on the "
    "storefront.",
    docs_url="https://shopify.dev/docs/api/admin-graphql/latest/mutations/translationsRegister",
    supports_oauth=False,
)


# shopping cart
class ShopifyTranslationStrategy(LocaleMappingStrategy):
    """T2-flavored write-back for Shopify's identity-less translations (see the module
    docstring for why the shared SubResourceStrategy's 404-probe + create/update split
    does not fit). Vendor-neutral in the same spirit as the shared strategies: it drives
    a small client protocol the connector implements. These are connector methods called
    across the strategy/connector boundary (kept on the connector because they need its
    authed GraphQL client):

        connection_id                              -> keys the shared D1 write lock
        resolve_locale(locale) -> exact code        (lower-cased flow locale -> wire case)
        source_content(resource_id) -> {key:{value,digest,type}} | ABSENT
        existing_translation_keys(resource_id, exact_locale) -> set[str]
        fully_covered(resource_id, exact_locale) -> bool   (every SELECTED key registered)
        register_translations(resource_id, exact_locale, inputs) -> _RegisterOutcome
        _record_incomplete(resource_id, locale, {key: reason})
        _record_registered(resource_id, locale, keys)

    `fields` is {key: {"value": translated, "src": source_text_at_translation_time}}.
    write_target registers ONLY the translated keys, EACH carrying a digest RE-READ fresh
    immediately before the mutation (gotcha #2 — the reference's stale download-time digest
    is NOT ported). A fresh digest alone would LAUNDER a stale translation past Shopify's
    staleness gate, so PER KEY we compare the freshly-read SOURCE TEXT to the text the
    translation was made from; a DRIFTED key is DROPPED (not registered) and recorded
    incomplete, and — because its source change alters the flow's content hash — it is
    re-selected automatically next run (HIGH-2, per-key so one flapping field never
    livelocks the whole resource). translationsRegister carries a per-key digest, so there
    is no cross-key atomicity to preserve; all-drifted falls through the `if not inputs`
    abort. A non-digest userError attributed to specific key(s) DROPS those and registers
    the rest (MEDIUM-1). NOTE: a held/poison-dropped key is NOT auto-re-attempted (it is
    never registered, so the full-coverage probe treats the pair as covered) — recovery is
    operator-only via retry (an explicit only_pairs retry makes the flow bypass suppression
    for the pair), OR a source edit; only a DRIFTED key (whose source changed) auto-returns
    via the hash change. A legitimately-identical
    translation is registered like any other (gotcha #3)."""

    topology = "T2"

    def exists_target(self, client, record_id: str, target_locale: str) -> bool:
        """True iff every key we PREVIOUSLY REGISTERED for this (resource, locale) is still
        present in the live overlay — FULL coverage keyed on the persisted registered-set,
        not a bare any-key probe (MEDIUM-3). A bare any-key probe would leave a single
        externally-deleted key's translation unrestored forever while the content hash
        matches; requiring the tracked set means a deleted key surfaces for a re-attempt.
        Untracked ⇒ NOT covered (no false-covered hole; costs at most one re-register that
        re-establishes tracking). An explicit server signal, never a content comparison."""
        exact = client.resolve_locale(target_locale)
        return client.fully_covered(record_id, exact)

    def write_target(
        self, client, record_id: str, target_locale: str, fields: dict, *, draft: bool = True
    ) -> WriteResult:
        # `draft` is accepted for the framework signature but ignored: a registered
        # translation is live immediately (there is no draft/publish at the translation
        # level; publish is a per-LOCALE action in Shopify admin — digest §3).
        if not fields:
            # Every translatable key held back (NEEDS_REVIEW). Write NOTHING and report
            # held-back so the flow records NO suppression hash; recovery is a source edit
            # or an operator retry (taxonomy F).
            return WriteResult(False, False, message=_HELD_BACK)
        # Serialize the exists->register span for this (connection, resource, locale) so
        # two in-process writers of the SAME pair don't both register concurrently
        # (design D1 lock; same single-process caveat as the shared strategies). Shopify's
        # upsert means a duplicate is harmless, but the lock keeps created/updated
        # accounting honest and the digest re-read + register atomic per pair.
        lock = _write_lock_for(getattr(client, "connection_id", None), record_id, target_locale)
        with lock:
            exact = client.resolve_locale(target_locale)
            existed = bool(client.existing_translation_keys(record_id, exact))
            pending = dict(fields)  # {key: {"value","src"}}; drifted/poison keys drop out
            held: dict = {}  # drifted / source-vanished / poison keys -> incomplete
            digest_retries = 0
            saw_digest = False
            last_digest_msg = ""
            total_wait = 0.0
            for _iteration in range(_MAX_DIGEST_RETRIES + _MAX_POISON_DROPS):
                # GOTCHA #2: re-read the CURRENT source content (values + digests) fresh
                # RIGHT NOW, every attempt — never a digest carried from discovery.
                src = client.source_content(record_id)
                if src is ABSENT:
                    return WriteResult(
                        False, False, message=f"resource {record_id} not found (deleted?)"
                    )
                inputs: list[dict] = []
                for key, spec in pending.items():
                    info = src.get(key)
                    if not info or not info.get("digest"):
                        # Source key vanished/renamed since discovery -> no valid digest.
                        held[key] = "source key not present with a digest (source changed?)"
                        continue
                    # HIGH-2 (per key): if THIS key's current source differs from the text
                    # its translation was made from, DROP it (never launder a stale
                    # translation) and record it incomplete. Its source change alters the
                    # flow's content hash, so it is re-selected automatically next run — one
                    # flapping field can't livelock the whole resource.
                    if (info.get("value") or "") != (spec.get("src") or ""):
                        held[key] = "source changed during translation"
                        continue
                    inputs.append(
                        {
                            "locale": exact,
                            "key": key,
                            "value": spec["value"],
                            "translatableContentDigest": info["digest"],
                        }
                    )
                if not inputs:
                    # All keys drifted / vanished / poison -> nothing coherent to write.
                    # Retryable, NO hash; the changed-source keys re-select next run.
                    return WriteResult(
                        False,
                        not existed,
                        message=(
                            "no writable keys (every key drifted, vanished or was rejected): "
                            + "; ".join(f"{k} ({v})" for k, v in sorted(held.items()))
                        ),
                    )
                out = client.register_translations(record_id, exact, inputs)
                if out.ok:
                    client._record_registered(record_id, target_locale, [i["key"] for i in inputs])
                    if held:
                        client._record_incomplete(record_id, target_locale, held)
                    return WriteResult(
                        True,
                        not existed,
                        target_id=record_id,
                        message="created" if not existed else "updated",
                    )
                if out.invalid_digest:
                    # A residual (non-drift) INVALID_DIGEST race — the drift check was clean
                    # but the source moved in the register window. Re-read + retry on its OWN
                    # bounded budget (M2); the next iteration's per-key drift-drop resolves it.
                    saw_digest = True
                    last_digest_msg = out.message
                    if (
                        digest_retries < _MAX_DIGEST_RETRIES - 1
                        and total_wait < _MAX_TOTAL_DIGEST_WAIT
                    ):
                        digest_retries += 1
                        wait = min(base.BACKOFF_BASE * (2**digest_retries), base.MAX_SLEEP)
                        wait = min(wait, _MAX_TOTAL_DIGEST_WAIT - total_wait)
                        total_wait += wait
                        base._sleep(wait)
                        continue
                    return WriteResult(
                        False,
                        not existed,
                        message=(
                            f"digest mismatch after {digest_retries + 1} attempt(s) (source keeps "
                            f"changing under the write): {last_digest_msg}"
                        ),
                    )
                # MEDIUM-1: a non-digest userError. Drop the key(s) Shopify attributes the
                # error to (on the poison-drop budget, M2), register the good remainder, and
                # record the dropped key(s) as incomplete — ONE poison key never blocks the
                # others. An unattributable error fails the whole pair (retryable, no hash).
                bad = set(out.bad_keys or ()) & set(pending)
                remainder = set(pending) - bad
                if bad and remainder:
                    for k in sorted(bad):
                        held[k] = f"rejected by Shopify: {out.message}"
                        pending.pop(k, None)
                    continue  # re-register the good remainder (re-reads + re-checks drift)
                return WriteResult(False, not existed, message=out.message)
            # Combined budget exhausted (many one-at-a-time rejections). Name what actually
            # happened rather than a misleading INVALID_DIGEST tail (M2).
            tail = (
                f"digest mismatch after {digest_retries + 1} attempt(s): {last_digest_msg}"
                if saw_digest
                else "translationsRegister did not converge after repeated key rejections"
            )
            return WriteResult(False, not existed, message=tail)


# ===========================================================================
# Translate flow — thin hooks over the shared engine (flow.run_translate_flow).
# Poll-only (no watermark): the content-hash gate is the change detector.
# ===========================================================================
def _shopify_preflight(ctx):
    # HIGH-3: the source we translate FROM is ALWAYS the shop's primary locale. If the
    # informational `source_locale` config is set and does NOT resolve to the primary, fail
    # LOUDLY (naming the primary) rather than silently mislabelling primary text as another
    # language and turning the primary into a writable target.
    conn = ctx.connector
    enabled = set(conn.enabled_locales())
    primary = conn._primary_locale().lower()
    cfg_src = (conn.config.get("source_locale") or "").strip()
    if cfg_src and conn.resolve_locale(cfg_src).lower() != primary:
        raise ConnectorError(
            f"Shopify translates FROM the shop's primary locale ({primary!r}); the "
            f"configured source_locale {cfg_src!r} does not match it. Shopify's source "
            f"content is always the primary locale — clear source_locale or set it to "
            f"{primary!r}."
        )
    if primary not in enabled:  # defensive: shopLocales always lists the primary
        raise ConnectorError(
            f"The shop's primary locale {primary!r} is not in shopLocales "
            f"({', '.join(sorted(enabled)) or 'none'})."
        )
    return enabled, {"source": primary, "primary": primary}


def _shopify_partition(locales, enabled, extra):
    """usable = enabled targets minus the PRIMARY locale; rejected = not enabled. The
    primary (== the source Shopify authors in) is UNCONDITIONALLY dropped from targets
    (HIGH-3) — never a writable target — and surfaced honestly as a source drop."""
    primary = (extra or {}).get("primary", "")
    rejected = [l for l in locales if l not in enabled]  # noqa: E741
    usable = [l for l in locales if l in enabled and l != primary]  # noqa: E741
    return rejected, usable


def _shopify_prepare(ctx, art):
    title_key, title_text, body_units, chash, body_keys = ctx.connector.extract(art)
    if not title_text.strip() and not body_units:
        return flow.SkipArticle("empty_skipped")
    rid = art["resource_id"]
    # HIGH-2: remember, per key, the SOURCE TEXT each translation is made from, so
    # write_target can abort if the live source has drifted before it registers.
    src_by_key = {u.p_index: u.text for u in body_units}
    if title_key is not None:
        src_by_key[title_key] = title_text
    return flow.ArticlePrep(
        aid=rid,
        chash=chash,
        title_text=title_text,
        body_units=body_units,
        extra_skeleton={
            "article_id": rid,
            "title": title_text or None,
            "resource_type": art.get("_rt"),
        },
        doc_title=f"{(title_text or art.get('_rt') or 'resource')[:50]} ({rid})",
        extra={
            "title_key": title_key,
            "body_keys": body_keys,
            "src_by_key": src_by_key,
            "connector": ctx.connector,
        },
    )


def _shopify_is_suppressed(connector, aid, loc):
    return connector.strategy.exists_target(connector, aid, loc)


def _shopify_on_write_ok(ctx, prep, loc):
    """H1: persist THIS pair's registered-key set NOW — per successful pair, BEFORE the
    end-of-run hash merge (flow stage 10) — so a crash can NEVER leave a hash without its
    tracking (which the untracked=>not-covered probe would otherwise re-register, but the
    old build_result timing left a permanent false-covered hole). Unioned under the config
    lock (patch_config_nested 'union'), so a concurrent run's delta for the same key
    survives (Shopify L1)."""
    from app.services.connectors import store

    map_key = f"{prep.aid}:{loc}"
    keys = (getattr(ctx.connector, "_registered_delta", {}) or {}).get(map_key)
    if keys:
        store.patch_config_nested(
            ctx.cid, {"shopify_registered": {"union": {map_key: sorted(keys)}}}
        )


def _shopify_build_payload(prep, loc, seg0_target, mapping):
    """The write payload for one locale: {key: translatedValue} for the keys actually
    translated this run. A HELD-BACK (NEEDS_REVIEW) key is OMITTED entirely — never
    source-seeded — so the stored translation (if any) survives (taxonomy B). The strategy
    writes NOTHING when this dict is empty (all keys held back -> held_back path).

    GOTCHA #3: a key whose translation equals the SOURCE is NOT filtered out here — it is
    included and registered like any other, so it is never perpetually re-picked.

    Each entry is {key: {"value": translated, "src": source_text}} so write_target can
    detect source drift before registering (HIGH-2)."""
    title_key = prep.extra["title_key"]
    src_by_key = prep.extra["src_by_key"]
    out: dict = {}
    if title_key is not None and seg0_target is not None:
        out[title_key] = {"value": seg0_target, "src": src_by_key.get(title_key, "")}
    for key in prep.extra["body_keys"]:
        if key in mapping:
            out[key] = {"value": mapping[key], "src": src_by_key.get(key, "")}
    # Operator-visible INCOMPLETE marker — only on a PARTIAL write (out non-empty). An
    # all-held write returns {} and stays the taxonomy-F held_back path (no double count).
    if out:
        candidates = ([title_key] if title_key else []) + list(prep.extra["body_keys"])
        held = {k: "awaiting review (NEEDS_REVIEW)" for k in candidates if k not in out}
        if held:
            prep.extra["connector"]._record_incomplete(prep.aid, loc, held)
    return out


def _is_held_back(f):
    return str((f or {}).get("error", "")).startswith(_HELD_BACK_PREFIX)


def _dropped_as_source(ctx):
    return [l for l in ctx.locales if l not in ctx.usable and l not in ctx.rejected]  # noqa: E741


def _confirmed_incomplete(ctx):
    """(resource, locale) pairs PARTIALLY written this run — some keys landed, others were
    held/poison/drift-dropped — CONFIRMED against the run's real failures (a pair whose
    write failed is a failure, not an incomplete). Operator-visible. A held/poison key was
    never registered, so the full-coverage probe treats the pair as COVERED: it is NOT
    auto-re-attempted — recovery is retry_failed (which passes the pair as only_pairs, and
    the flow bypasses suppression for it) or a source edit. (A drift-dropped key's source
    already changed, so it auto-returns via the hash.)"""
    failed = {(str(f.get("article_id")), str(f.get("locale"))) for f in ctx.failures}
    out = []
    for (aid, loc), held in (getattr(ctx.connector, "_incomplete", {}) or {}).items():
        if (aid, loc) in failed:
            continue
        out.append(
            {
                "article_id": aid,
                "locale": loc,
                "held_fields": sorted(held.keys()),
                "reasons": dict(held),
            }
        )
    return out
