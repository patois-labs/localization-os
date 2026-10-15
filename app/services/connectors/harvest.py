"""Connector context harvest — SPEC-context-metadata Phase 3, PART A.

THE DIFFERENTIATOR, mechanized: when a connector creates a Document at INGESTION,
map a SMALL, EXPLICIT allowlist of the source system's already-in-hand metadata
(title, section, labels, ...) onto a TranslationContext `source` block, route it
through `context_pkg.validate_and_sanitize` (the SAME allowlist + fence-neutralize
+ cap contract the API/web upload path uses), and return a clean dict for
`_new_connector_doc(context=...)` to stamp on `Document.context`. Phase 2A's
`context_pkg.resolve(document=doc, ...)` then injects it as the DOCUMENT layer of
every segment prompt with ZERO per-connector translate change — closing the
harvest gap for six flow connectors + Airtable in one seam.

DESIGN LAW (audit + review):
  * ALLOWLIST ONLY — each connector maps a HANDFUL of NAMED fields onto the
    `source` block; the raw record is NEVER dumped. Keep each map tiny + explicit.
  * DATA-ONLY authority (tier 3, SPEC §Authority tiers): EVERY harvested value
    routes through `validate_and_sanitize` — nothing instruction-shaped, no
    unknown key, no fence break-out can ride along (the upload contract mirror).
  * NEW-FETCH fields land in PART B (this file now carries the Part-B mappers too):
    Jira `fields=` widening (project/issuetype/labels/summary), Salesforce RecordType
    NAME resolution (the opaque RecordTypeId -> content_kind), Zendesk section
    id->name (-> source.section), and the Confluence migration off its bespoke
    brief onto _map_confluence. Each Part-B fetch happens ONCE per pull (cached),
    never per-article, inside the existing egress-gated pull path; the RESOLVED name
    (never a raw id) is what reaches the mapper. Notion parent-page chain + Help
    Scout collectionId->name remain deferred (still title-only above).
  * BYTE-STABILITY (golden #1): harvest OFF (`harvest_context=0`) OR nothing
    harvestable -> return None (NEVER {}) so `Document.context` stays None,
    resolve() adds nothing, and the assembled prompt is byte-identical to a
    pre-harvest run. A present harvest legitimately CHANGES the prompt (adds the
    `<translation_context source="...">` block) — that is the whole point.

Confluence was migrated onto this mechanism in Part B: its bespoke
`_confluence_context_brief` seam is GONE and `_map_confluence` maps space name +
ancestor chain -> source.section (routed through validate_and_sanitize + layered
resolve() like every other connector). Its config flag `harvest_context` supersedes
the legacy `context_brief` (kept as a backward-compat OFF alias — see store.py).
"""

from __future__ import annotations

from app.services import context_pkg


def harvest_enabled(cfg) -> bool:
    """Per-connection gate: config flag `harvest_context` (default ON), mirroring
    Confluence's `context_brief` gate shape but a DISTINCT flag. Set 0/false/no to
    disable harvest for a connection (Document.context then stays None)."""
    return str((cfg or {}).get("harvest_context", "1")).strip().lower() not in ("0", "false", "no")


# The renderable source keys — the ones that produce a BODY line in render_block.
# `source.system` is provenance ONLY (it renders into the `source="..."` ATTRIBUTE,
# never a body line) and `source.url` is never rendered — so a package carrying
# ONLY those is content-free and MUST resolve to None, or a connector run with no
# harvestable title/labels/section would still inject an attribute-only
# `<translation_context source="zendesk"></translation_context>` block and break
# the byte-stability contract (golden #1).
_RENDERABLE_SOURCE = ("title", "section", "labels")
_RENDERABLE_TOP = (
    "domain",
    "content_kind",
    "audience",
    "formality",
    "tone",
    "char_limit",
    "notes",
    "custom",
)


def _has_renderable(pkg: dict) -> bool:
    """True iff the sanitized package carries at least one field that render_block
    emits as BODY content (so it would actually change a prompt). A bare
    source.system / source.url does NOT count."""
    src = pkg.get("source") or {}
    return any(src.get(k) for k in _RENDERABLE_SOURCE) or any(pkg.get(k) for k in _RENDERABLE_TOP)


def _clean(raw) -> dict | None:
    """Route a RAW harvest dict through the ingestion contract
    (`context_pkg.validate_and_sanitize`) and normalize a content-free result to
    None (byte-stability: None keeps Document.context None). A harvest map should
    never produce an out-of-contract dict, but if a value somehow violated the
    contract we fail SAFE (no context) rather than break ingestion. A package that
    sanitizes to nothing renderable (e.g. only `source.system` survived) also
    returns None — provenance alone is not context worth a prompt block."""
    if not raw:
        return None
    try:
        clean = context_pkg.validate_and_sanitize(raw)
    except context_pkg.ContextError:
        return None
    if not clean or not _has_renderable(clean):
        return None
    return clean


def _source(system, *, title=None, section=None, labels=None) -> dict:
    """Assemble ONLY the present allowlisted `source` keys (system always; title /
    section / a non-empty label-name list when in hand). validate_and_sanitize
    caps + neutralizes each value afterwards."""
    src: dict = {"system": system}
    if title:
        src["title"] = title
    if section:
        src["section"] = section
    if labels:
        names = [x for x in labels if x]
        if names:
            src["labels"] = names
    return src


# --- per-connector-kind harvest maps (SMALL + explicit, EASY FIELDS ONLY) -----
# Each takes an in-hand `fields` dict and returns a RAW TranslationContext dict
# (pre-sanitize). Only fields already fetched at ingestion — no new API call.


def _map_zendesk(f):
    # label_names is already fetched as a list of NAMES (zendesk.py:306) -> labels.
    # section (Part B) is the article's section id RESOLVED to its name once-per-pull
    # by the flow (store._zd_prepare via connector.section_name) -> source.section.
    return {
        "source": _source(
            "zendesk", title=f.get("title"), section=f.get("section"), labels=f.get("labels")
        )
    }


def _map_document360(f):
    raw = {"source": _source("document360", title=f.get("title"))}
    editor = f.get("editor")  # markdown|html — the article's editor type
    if editor:
        raw["custom"] = {"editor": editor}
    return raw


def _map_freshdesk(f):
    return {"source": _source("freshdesk", title=f.get("title"))}


def _map_zoho(f):
    return {"source": _source("zoho", title=f.get("title"))}


def _map_intercom(f):
    # default_locale is provenance, not translation context — deliberately skipped.
    return {"source": _source("intercom", title=f.get("title"))}


def _map_salesforce(f):
    # Part B: the article's RecordTypeId is RESOLVED to its RecordType NAME once-per-pull
    # by the flow (store._sf_prepare via connector.record_type_name) and passed as
    # `content_kind`. Title+system are the 3A easy fields.
    raw = {"source": _source("salesforce", title=f.get("title"))}
    if f.get("content_kind"):
        raw["content_kind"] = f["content_kind"]
    return raw


def _map_jira(f):
    # Part B: the widened Jira metadata fetch (store.py adds project,issuetype,labels
    # to fields= WITHOUT making them translatable units). project name -> domain,
    # issuetype name -> content_kind, summary -> source.title, labels -> source.labels.
    # store._jira_harvest_fields resolves the multi-issue-per-document ambiguity: it
    # supplies domain/content_kind only when the whole run shares ONE value, and
    # title/labels only for a single-issue run (an aggregate title would be meaningless).
    raw = {"source": _source("jira", title=f.get("title"), labels=f.get("labels"))}
    if f.get("domain"):
        raw["domain"] = f["domain"]
    if f.get("content_kind"):
        raw["content_kind"] = f["content_kind"]
    return raw


# Confluence: cap the ancestor chain (closest-to-page ancestors carry the most
# section context) to mirror the old brief's _BRIEF_MAX_ANCESTORS bound before the
# combined string hits validate_and_sanitize's SHORT_CAP.
_CONF_MAX_ANCESTORS = 5


def _map_confluence(f):
    # Part B migration (replaces _confluence_context_brief): space name + ancestor-title
    # chain -> ONE source.section string; page labels -> source.labels; title ->
    # source.title. Routes through validate_and_sanitize (allowlist + fence-neutralize +
    # SHORT_CAP) and layered resolve() like every other connector — so a project/customer
    # card can now OVERRIDE Confluence context (the old brief structurally could not).
    # Separator is " / " (a breadcrumb), NOT " > ": validate_and_sanitize entity-encodes
    # `>` (fence-break defense) to `&gt;`, which would render the path as noisy
    # "A &gt; B". A slash is ASCII, un-escaped, and reads cleanly as a section path.
    #
    # NEAREST-first survival (MED-1): validate_and_sanitize FRONT-truncates a value at
    # SHORT_CAP, so a breadcrumb ordered space -> far-ancestor -> ... -> immediate-parent
    # would lose the IMMEDIATE PARENT (the most relevant section context) on a deep page.
    # We keep hierarchy order (space first, then root..parent) for readability but, when
    # the joined breadcrumb would exceed SHORT_CAP, drop the FARTHEST ancestor first
    # (front of the chain, right after the space) until it fits — so the nearest
    # ancestors always survive.
    space = (f.get("space") or "").strip()
    chain = [a for a in (f.get("ancestors") or []) if a][-_CONF_MAX_ANCESTORS:]
    section = None
    while True:
        parts = ([space] if space else []) + chain
        section = " / ".join(parts) if parts else None
        if not section or len(section) <= context_pkg.SHORT_CAP or not chain:
            break
        chain = chain[1:]  # drop the FARTHEST ancestor; keep the nearest ones
    return {
        "source": _source(
            "confluence", title=f.get("title"), section=section, labels=f.get("labels")
        )
    }


def _map_notion(f):
    # Parent-page chain (section) needs a fetch -> Part B; only the page title is
    # in hand at prepare time.
    return {"source": _source("notion", title=f.get("title"))}


def _map_helpscout(f):
    # collectionId->name resolution = Part B (only the opaque collection id is in
    # hand); harvest the article title only.
    return {"source": _source("helpscout", title=f.get("title"))}


def _map_strapi(f):
    # The entry's title field is in hand at prepare time; the content type is an
    # opaque plural API id (not a human section name) -> deliberately skipped.
    return {"source": _source("strapi", title=f.get("title"))}


def _map_contentful(f):
    # The entry's display-field title is in hand at prepare time; the content type is
    # an API identifier (not a human section name) -> deliberately skipped.
    return {"source": _source("contentful", title=f.get("title"))}


def _map_shopify(f):
    # The resource's title key is in hand at prepare time; the resource type is a generic
    # enum (PRODUCT/COLLECTION/...), not a human section name -> deliberately skipped.
    return {"source": _source("shopify", title=f.get("title"))}


def _map_hubspot(f):
    # The object's `name` is in hand at prepare time; the content type is a generic
    # enum (site-pages/blog-posts), not a human section name -> deliberately skipped.
    return {"source": _source("hubspot", title=f.get("title"))}


def _map_wordpress(f):
    # The post/page title is in hand at prepare time; the post TYPE is a generic route
    # segment (posts/pages), not a human section name -> deliberately skipped.
    return {"source": _source("wordpress", title=f.get("title"))}


def _map_akeneo(f):
    # The record's display title (when resolved) is in hand at prepare time under the "title"
    # key (build_flow always passes it there); the content type is an opaque id (not a human
    # section name) -> deliberately skipped. (The generated skeleton emitted f.get("None") from
    # the null harvest_title_field template bug; corrected here to match every sibling mapper.)
    return {"source": _source("akeneo", title=f.get("title"))}


def _map_mcp_cms(f):
    # Generated: the item's title is in hand at prepare time; the content type is
    # an opaque id (not a human section name) -> deliberately skipped.
    return {"source": _source("mcp_cms", title=f.get("title"))}


def _map_gdocs(f):
    # Generated: the item's title is in hand at prepare time; the content type is
    # an opaque id (not a human section name) -> deliberately skipped.
    return {"source": _source("gdocs", title=f.get("title"))}


def _map_servicenow(f):
    # Generated: the item's title is in hand at prepare time; the content type is
    # an opaque id (not a human section name) -> deliberately skipped.
    return {"source": _source("servicenow", title=f.get("title"))}


def _map_msgraph(f):
    # Generated: the item's title is in hand at prepare time; the content type is
    # an opaque id (not a human section name) -> deliberately skipped.
    return {"source": _source("msgraph", title=f.get("title"))}


def _map_plytix(f):
    # Generated: the item's title is in hand at prepare time; the content type is
    # an opaque id (not a human section name) -> deliberately skipped.
    return {"source": _source("plytix", title=f.get("title"))}


def _map_drupal(f):
    # Generated: the item's title is in hand at prepare time; the content type is
    # an opaque id (not a human section name) -> deliberately skipped.
    return {"source": _source("drupal", title=f.get("title"))}


def _map_github(f):
    # Generated: the item's title is in hand at prepare time; the content type is
    # an opaque id (not a human section name) -> deliberately skipped.
    return {"source": _source("github", title=f.get("title"))}


def _map_sanity(f):
    # Generated: the item's title is in hand at prepare time; the content type is
    # an opaque id (not a human section name) -> deliberately skipped.
    return {"source": _source("sanity", title=f.get("title"))}


def _map_sitecore(f):
    # Generated: the item's title is in hand at prepare time; the content type is
    # an opaque id (not a human section name) -> deliberately skipped.
    return {"source": _source("sitecore", title=f.get("title"))}


def _map_bitbucket(f):
    # Generated: the item's title is in hand at prepare time; the content type is
    # an opaque id (not a human section name) -> deliberately skipped.
    return {"source": _source("bitbucket", title=f.get("title"))}


def _map_gitlab(f):
    # Generated: the item's title is in hand at prepare time; the content type is
    # an opaque id (not a human section name) -> deliberately skipped.
    return {"source": _source("gitlab", title=f.get("title"))}


# factory:anchor harvest.mappers  (scripts/factory inserts new _map_<kind> functions above this line)  # noqa: E501


def _map_airtable(f):
    # The Airtable TABLE name is a real, in-hand name (from the request/config),
    # not an opaque id -> source.section. The batch webhook doc spans many records,
    # so there is no single meaningful doc title to harvest.
    return {"source": _source("airtable", section=f.get("section"))}


_HARVESTERS = {
    "zendesk": _map_zendesk,
    "document360": _map_document360,
    "freshdesk": _map_freshdesk,
    "zoho": _map_zoho,
    "intercom": _map_intercom,
    "salesforce": _map_salesforce,
    "gdocs": _map_gdocs,
    "jira": _map_jira,
    "confluence": _map_confluence,
    "notion": _map_notion,
    "helpscout": _map_helpscout,
    "airtable": _map_airtable,
    "strapi": _map_strapi,
    "contentful": _map_contentful,
    "shopify": _map_shopify,
    "hubspot": _map_hubspot,
    "wordpress": _map_wordpress,
    "akeneo": _map_akeneo,
    "mcp_cms": _map_mcp_cms,
    "servicenow": _map_servicenow,
    "msgraph": _map_msgraph,
    "plytix": _map_plytix,
    "drupal": _map_drupal,
    "github": _map_github,
    "sanity": _map_sanity,
    "sitecore": _map_sitecore,
    "bitbucket": _map_bitbucket,
    "gitlab": _map_gitlab,
    # factory:anchor harvest.harvesters
}


def build(kind: str, fields: dict | None, cfg) -> dict | None:
    """PUBLIC harvest entry: for connector `kind`, map its in-hand `fields` onto a
    sanitized TranslationContext dict ready for `_new_connector_doc(context=...)`,
    or None when harvest is disabled for the connection, the kind is unregistered,
    or nothing harvestable was in hand. The returned dict is ALREADY the clean
    output of validate_and_sanitize (the seam does not re-validate)."""
    if not harvest_enabled(cfg):
        return None
    mapper = _HARVESTERS.get(kind)
    if mapper is None:
        return None
    return _clean(mapper(fields or {}))


def build_flow(prep, cfg, kind: str) -> dict | None:
    """Harvest for a `flow.ArticlePrep`-driven connector (Zendesk / Document360 /
    Freshdesk / Zoho / Intercom / Salesforce / Notion / Help Scout): assemble the
    `fields` from data the prepare hook ALREADY captured — the title from the
    persisted skeleton (or title_text), the Document360 editor type, and any
    connector-specific extras the hook placed on `prep.harvest` (Zendesk labels) —
    then delegate to build(). Called from flow._materialize, so ONE wiring covers
    every flow connector."""
    skel = prep.extra_skeleton or {}
    fields = {
        "title": skel.get("title") or prep.title_text or None,
        "editor": skel.get("editor"),
    }
    fields.update(prep.harvest or {})
    return build(kind, fields, cfg)
