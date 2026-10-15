"""WordPress (Polylang) connector - the THIRD T3 (document-per-locale) consumer of
the connector framework (connector-factory hand-build #5).

TOPOLOGY - T3, doc-per-locale via Polylang, RE-DERIVED (not trusted) from the digest
§3/§7: each language is a WHOLLY SEPARATE `wp_posts` row with its own ID, slug, status
and content - there is no field-level or sub-resource localization inside one post
(confirmed at the data-shape level: Polylang exposes a translation as an entirely
different post fetched by a different id). This is structurally identical to Notion
(duplicate page per locale) and Help Scout (article per language), so write-back rides
the SAME vendor-neutral `strategy.DocPerLocaleStrategy` + `idmap` reserve-then-fill +
7-case reconciliation lifecycle those two built. This module supplies only the four
T3DocClient methods (create/fill/verify/find_preexisting), WordPress's auth + endpoints,
and the WordPress-specific fidelity/posture handling below. Zero engine edits.

OUR OWN PERSISTED ID MAP IS THE SOURCE OF TRUTH, ALWAYS (digest §7). Polylang PRO
exposes its internal link over REST as `translations: {locale: id}` on every post/page
plus writable `?lang=`/`?translations[xx]=` params - an "assisted T3" corroboration
signal. But that assistance is (a) Pro-gated and (b) only ever a CROSS-CHECK: exists_target
reads OUR `done` map row, never WordPress's translations map. Without Pro the connector
degrades to pure uncorroborated T3 (our map is the only truth) - identical to Notion's
worst case.

THE FOUR DIGEST GOTCHAS ARE DESIGN REQUIREMENTS (digest §6):

  1. CONTENT FIDELITY - READ/WRITE `content.raw`, NEVER `content.rendered` (digest §6.1,
     the sharpest divergence-from-reference). The reference reads `content.rendered` and
     writes it back as raw `post_content`, which FLATTENS Gutenberg blocks (the
     `<!-- wp:paragraph -->` block markers exist only in raw content; rendering strips
     them) and BAKES IN shortcode output permanently on one round trip. Our port requests
     `?context=edit` (which makes WP include the `raw` sub-field, gated on the user's
     edit capability) and reads/writes `content.raw` + `title.raw`, so a Gutenberg-block
     post survives a full pull->translate->push cycle byte-structurally (the raw body routes
     through the shared `html` filter, which treats the block-comment markers as skeleton and
     translates only leaf text). SHORTCODES need a further step (H3): a raw
     `[contact-form-7 ...]` IS a leaf text node to the html filter, so left alone it would be
     SENT TO THE ENGINE and mangled (attribute values corrupted). Our port MASKS every
     shortcode bracket span as a protected placeholder (an HTML comment the filter preserves
     as skeleton) BEFORE segmentation and UNMASKS it verbatim after rebuild, so a shortcode
     (and its attribute values) never reaches the engine and round-trips byte-identical; the
     inner text of an enclosing shortcode stays translatable (mutation-proven in the verify
     suite with a rewriting fake engine). If a post's `content.raw` is UNAVAILABLE (the
     response carried only `rendered`, i.e. the connection's user lacks edit capability), the
     post is SKIPPED with a loud error - we never fall back to translating rendered HTML.

  2. PRO-ABSENCE SELF-DETECTION (digest §6.2). The free/Pro split is near-total: without
     Polylang Pro there are NO `lang`/`translations` fields on any response, and the
     reference NPEs on the null `translations` map. We DETECT Pro presence ourselves at
     connect/test/run time (`_polylang_mode`: probe `pll/v1/languages`, then probe whether
     a sample post's edit-context response actually carries a `translations` field),
     degrade to pure uncorroborated T3 when Pro is absent, NEVER crash on a null/absent
     `translations` field, and SURFACE the detected mode operator-visibly (the connection
     page id-map panel + the CLI/web `test` message + the run message) - not a log line.
     The mode is persisted on `config['wordpress_polylang_mode']` by the run preflight and
     the test path so the connection page can render it without a live call.

  3. DRAFT/PUBLISH POSTURE - EXPLICIT `status` ON BOTH POSTS AND PAGES, reads/probes/writes
     target the SAME state (digest §6.3, the Strapi-H4 shape via a missing param). The
     reference gives POSTS a `status` param but PAGES none, and its create defaults to
     `draft` while its Search never sends `status=` (WP's REST default is publish-only) -
     so a created page lands as an un-promotable draft INVISIBLE to the connector's own
     search forever. Our port: every CREATE (posts AND pages) sends an explicit `status`
     (config `publish`: 1 -> "publish", 0/default -> "draft"); an UPDATE OMITS status (M1 -
     WP's partial update keeps the target's current status, so re-translating never
     un-publishes a human-published translation); DISCOVERY sends an explicit
     `status=<source_status>` (default "publish", never the REST default); VERIFY/PROBE reads
     by id with `context=edit`, which returns the post regardless of its status (so a draft
     target we created is always findable - the H4 fix). exists_target reads OUR id map (a
     `done` row) but is corroborated by a cheap live GET (H2) so an externally deleted /
     blanked target is recreated/refilled rather than suppressed forever; the second-run
     steady state for BOTH types is: source unchanged + target healthy -> suppressed; our own
     freshly-created targets are EXCLUDED from discovery (see below), so they are never
     re-discovered as sources to translate.

  4. CORRECT PAGINATION (digest §6.5/§5). The reference's `page++` post-increment do-while
     silently DROPS THE LAST PAGE of every multi-page result. We loop
     `while page <= total_pages` off the `X-WP-TotalPages` header (case-insensitive), so
     exactly-one-page, exactly-full-page and one-over-a-page all return every item (boundary
     tests in the verify suite).

DISCOVERY SCOPE + SELF-TARGET EXCLUSION (B1, pure-T3 steady state). A translation we create
is itself an ordinary `wp_posts` row, so discovery must not re-list it (or any OTHER-language
content) as a source. TWO layers, because the id-map exclusion alone is NOT sufficient (it
only removes posts WE created):
  * With Pro, discovery filters `lang=<source>` server-side - the reliable language signal.
  * WITHOUT Pro there is NO per-post language signal, so whole-site auto-discovery is REFUSED
    fail-closed unless the operator configures an explicit source SCOPE (`source_category` or
    `source_tag` id); discovery then runs WITHIN that scope only. Explicit post ids are always
    allowed. (Runbook keeps a deferred live check: if free Polylang turns out to expose any
    language signal, this can loosen later.)
  * In ALL cases we ALSO exclude any post whose `<type>:<id>` ref is a known TARGET id in our
    id map (draft or published; and an explicitly-named own-target id is refused too, M4).

AUTH - WordPress core Application Passwords (built into WP 5.6+, not a plugin): HTTP Basic
where the "password" is a per-user revocable app token (wp-admin -> Users -> Profile).
Self-host note (digest §2): WP core gates the Application Passwords UI behind HTTPS unless
the host is recognized as local; and a security plugin can disable `/wp-json/wp/v2/users`.
Neither is checkable offline - see the runbook.

STATE GROWTH NOTE (round-2 L2): the connector persists a small per-target content hash in
`config['wordpress_written']` (M2 human-edit honesty) and a per-(post,locale) hash in
`config['wordpress_hashes']` (suppression). Both grow with the number of translated pairs and
are NEVER pruned here - bounded pruning of stale connector config maps is a FACTORY-LEVEL
concern (a shared chip), deliberately not built per-connector.

CHANGE DETECTION - POLL-ONLY, NO WATERMARK (digest §4). WordPress core has no content
webhook. We poll, but `modified`/`modified_gmt` are the content signal only loosely
(any touch bumps them), so - the standing CARDINAL RULE - no watermark is advanced off
them; the per-(post, locale) source-content HASH is the sole re-translate gate (the
Help Scout / Zoho precedent).

DELIBERATELY OUT OF SCOPE (digest §1/§6): categories/tags/menus/blocks endpoints (the
reference has none - no taxonomy translation), featured-image / custom-meta copy-forward
(Pro "Synchronized posts" only; a fresh translation starts bare - documented residual),
WordPress Multisite (orthogonal to Polylang; one flat site here), comments (half-built in
the reference), and full Pro translations-map DIVERGENCE reconciliation beyond the rule-6
pre-existing-target guard (a human relinking a source to a DIFFERENT id in wp-admin is a
documented live-QA residual - the runbook names it).

LIVE QA - DEFERRED by the maintainer's explicit exception (CONNECTOR-BUILD-BLUEPRINT §5 row 5):
full fidelity needs Polylang Pro (his cost call). This connector ships flagged
"live QA pending" - see the connector's QA notes. The verify suite's
fake WordPress/Polylang server models BOTH Pro-present and Pro-absent response shapes
(null translations on free; status-scoped queries; raw-vs-rendered) so the tests are
non-vacuous.

PROVENANCE: endpoint shapes are reference-and-reimplemented from bb-io/Wordpress
((c) 2023 Blackbird International Inc., MIT - verified verbatim at build time). Reference-only;
patterns ported, non-trivial code never copied. See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re

import httpx

from app.filters.html_filter import _is_tag_token, _parse_tag, _tokenize
from app.services.connectors import flow, harvest, idmap, webhooks  # noqa: F401
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
    _validate_target,
    fetch_cap,
    harvest_context_field,
    to_int,
)
from app.services.connectors.contract import (
    AuthKind,
    Capability,
    ChangeSignals,
    ConnectorPlugin,
    RichText,
    Topology,
)
from app.services.connectors.strategy import DocPerLocaleStrategy, TargetUnverifiable, WriteResult
from app.services.connectors.vendor_text import vendor_excerpt

log = logging.getLogger("loc_tms.connectors")

# WordPress REST namespaces (digest §1). The main content API is wp/v2; Polylang's one
# FREE surface (the Languages REST API, bundled since Polylang 3.7) is pll/v1.
_WP_NS = "wp/v2"
_PLL_NS = "pll/v1"

# The post TYPES this v1 supports - the REST route segment (plural). Posts AND pages both
# get the explicit-status treatment (digest §6.3). Operator config is validated against
# this set (taxonomy A), and an explicit ref's type is scope-checked against the
# connection's configured types.
_SUPPORTED_TYPES = ("posts", "pages")
_SUPPORTED_SET = frozenset(_SUPPORTED_TYPES)

# Source-discovery statuses the operator may choose. "any" needs edit capability (which the
# Application-Password user has). Validated so a typo can't reach the wire as a status.
_SOURCE_STATUS_SET = frozenset({"publish", "draft", "pending", "private", "future", "any"})

# Polylang detection outcomes. Only "pro" enables the assisted linkage (write `?lang=`/
# `?translations[xx]=` + read the `translations` map); "free"/"absent"/"unknown" all run
# pure uncorroborated T3 (our id map is the only truth). "unknown" = probed but no content
# existed to prove Pro either way -> treated as pure T3, surfaced honestly.
_MODE_PRO = "pro"
_MODE_FREE = "free"
_MODE_ABSENT = "absent"  # Polylang not installed at all
_MODE_UNKNOWN = "unknown"  # could not determine (no sample post) -> pure T3

# A real WP post id is a positive integer; cap the digit length so a pathological id can
# never reach the wire as a path segment / query var.
_ID_RE = re.compile(r"^[0-9]{1,18}$")

# Shortcode spans. A WordPress shortcode is `[name attrs]` / `[name]` (self-closing) or a
# `[name]...[/name]` pair - each BRACKET SPAN in a TEXT NODE is masked as a protected
# placeholder so its text (and its attribute VALUES, never translatable in v1) is never sent
# to the engine. The pattern is TIGHT (round-2 M1) to avoid eating prose brackets:
#   * the name must LOOK like a real shortcode name - WP registers `^[a-z0-9_-]+`, so we
#     require a LOWERCASE-first name (`[a-z][a-z0-9_-]*`). This excludes `[12]` (digit),
#     `[Note: ...]` (uppercase + colon).
#   * a no-attr form (`[gallery]`, `[/gallery]`, `[foo /]`) is allowed; an attr form must
#     contain a `=` (`[id="7"]`), which excludes `[citation needed]` (space + non-attr word).
# Residual masks (e.g. `[sic]`, which is shape-identical to a real no-attr shortcode) are
# COUNTED in the run report (like img-alt) so an operator can spot a false mask.
_SHORTCODE_RE = re.compile(r"\[/?[a-z][a-z0-9_-]*+(?:\s++[^\[\]=]*+=[^\[\]]*+)?\s*+/?\]")
# The mask sentinel is an HTML COMMENT, which the shared html filter preserves as skeleton
# (verbatim, never a translatable unit), so the shortcode round-trips byte-identical. Distinct
# from Gutenberg's `wp:` markers.
_SC_SENTINEL = "<!--wpsc:%d-->"
_SC_SENTINEL_RE = re.compile(r"<!--wpsc:(\d+)-->")
# Split a body into alternating TEXT (even indices) and TAG (odd indices) segments. Round-2
# H1: a shortcode inside an HTML ATTRIBUTE (`<a href="[site_url]/x">`) must NOT be masked - an
# HTML-comment sentinel's `>` would close the tag and destroy the element. Attributes never
# reach the engine anyway (they are skeleton), so a shortcode there is already safe.
# Image alt text (M5). The shared html filter translates `<img alt>` (and image/link
# `title` / `aria-label`) as their own units (output-a11y train, VPAT 504.2). ONE
# deliberate exception: an image inside a Gutenberg COVER block. core/cover keeps a SECOND
# copy of the alt in its block-comment JSON (`<!-- wp:cover {"alt":"..."} -->`); translating
# only the <img> copy would put the two out of sync and the block editor would report the
# block as invalid. So cover-block image alt/title/aria-label units are left out of
# translation and COUNTED, and the run says so (a true count, never a silent skip).
_COVER_OPEN_RE = re.compile(r"^<!--\s*wp:cover\b(?!.*/-->$)", re.S)
_COVER_CLOSE_RE = re.compile(r"^<!--\s*/wp:cover\b")


def _mask_shortcodes(text: str) -> tuple[str, list[str]]:
    """Replace every shortcode bracket span IN A TEXT NODE with a protected HTML-comment
    placeholder so it is never handed to the translation engine (H3). A shortcode inside an
    HTML tag/attribute is LEFT AS-IS (round-2 H1 - masking it would break the tag; it is
    skeleton and never reaches the engine anyway). Returns (masked_text, [shortcodes])."""
    codes: list[str] = []

    def _repl(m):
        codes.append(m.group(0))
        return _SC_SENTINEL % (len(codes) - 1)

    parts = _tokenize(text or "")
    skip_name = ""
    for i, part in enumerate(parts):
        if _is_tag_token(part):
            closing, name, _self_closing = _parse_tag(part)
            if closing and name == skip_name:
                skip_name = ""
            elif not skip_name and not closing and name in ("script", "style"):
                skip_name = name
        elif not skip_name:
            # A comment sentinel inside script can change its HTML escaped-data state.
            parts[i] = _SHORTCODE_RE.sub(_repl, part)
    return "".join(parts), codes


def _unmask_shortcodes(text: str, codes: list[str]) -> str:
    """Restore masked shortcodes verbatim after the html filter rebuilds the body (H3)."""

    def _repl(m):
        i = int(m.group(1))
        return codes[i] if 0 <= i < len(codes) else m.group(0)

    return _SC_SENTINEL_RE.sub(_repl, text or "")


def _sentinel_count(text: str) -> int:
    """Number of intact mask sentinels currently in `text` (round-2 H2 postcondition)."""
    return len(_SC_SENTINEL_RE.findall(text or ""))


def _cover_image_units(masked_body: bytes) -> set:
    """p_index of every IMAGE accessibility unit (alt / title / aria-label on <img> or
    <area>) that sits inside a Gutenberg core/cover block (M5, see _COVER_OPEN_RE). Those
    units are kept out of translation so the block's JSON alt and its <img alt> stay
    identical. Nested covers are tracked by depth."""
    from app.filters.html_filter import a11y_attr_units

    tokens, units = a11y_attr_units(masked_body)
    inside: set = set()
    depth = 0
    for ti, tok in enumerate(tokens):
        if _COVER_CLOSE_RE.match(tok):
            depth = max(0, depth - 1)
        elif _COVER_OPEN_RE.match(tok):
            depth += 1
        elif depth:
            inside.add(ti)
    return {u["p_index"] for u in units if u["ti"] in inside and u["style"].startswith("a11y:img")}


class _Unit:
    """A minimal flow translation unit (the engine reads only .text + .p_index). Used for
    the non-HTML excerpt field (p_index 'excerpt'); the HTML body units come straight from
    the html filter."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index: str):
        self.text = text
        self.p_index = p_index


def content_hash(*parts) -> str:
    """Stable per-post source-content hash for the poll-suppression gate. Covers the raw
    title + the raw (Gutenberg/shortcode-bearing) content body. Order-sensitive; SHA-256
    hex (truncated). A change to either re-runs; nothing else is a content signal here."""
    h = hashlib.sha256()
    for p in parts:
        h.update((str(p) if p is not None else "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:32]


def _parse_names(raw) -> list[str]:
    out: list[str] = []
    for x in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        n = x.strip()
        if n and n not in out:
            out.append(n)
    return out


def _raw_of(field):
    """Extract the RAW string from a WP REST content/title field (gotcha #1).

    Under `?context=edit` WP returns `{"raw": ..., "rendered": ...}`; we want `raw`. A
    plain string is accepted verbatim (some fields/plugins return a bare string). Returns
    (raw_str | None): None SPECIFICALLY signals "only rendered was available" (a dict with
    `rendered` but no `raw`) - the caller then SKIPS the post rather than ever translating
    rendered HTML (which would flatten Gutenberg blocks / bake in shortcodes)."""
    if field is None:
        return ""
    if isinstance(field, str):
        return field
    if isinstance(field, dict):
        if "raw" in field:
            return str(field.get("raw") or "")
        if "rendered" in field:
            return None  # edit context absent -> refuse (never translate rendered)
        return ""
    return str(field)


SPEC = ConnectorSpec(
    kind="wordpress",
    name="WordPress",
    icon="\U0001f4dd",  # memo
    cat="CMS / Blog",
    blurb="Translate WordPress posts and pages into any language using Polylang's "
    "doc-per-locale model. WordPress has no field-level localization, so each "
    "translation is created as a SEPARATE post/page in the target language and "
    "tracked in an internal id map, so the source and other locales are never "
    "overwritten. Reads and writes raw block content (context=edit), so Gutenberg "
    "blocks and shortcodes survive the round trip. Polylang Pro adds a corroborating "
    "translation link; without Pro the connector runs on its own id map alone. "
    "Live QA pending (Polylang Pro cost) - see the runbook.",
    config_fields=[
        FieldSpec(
            "base_url",
            "Site URL",
            "Your WordPress site root, e.g. "
            "https://blog.acme.com. The connector appends /wp-json. HTTPS is "
            "strongly recommended (WordPress disables Application Passwords over "
            "plain HTTP on non-local hosts).",
            placeholder="https://blog.acme.com",
        ),
        FieldSpec(
            "post_types",
            "Post types",
            "Comma-separated WordPress types to translate: any of posts, pages (default posts).",
            required=False,
            placeholder="posts, pages",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "Language of the source content "
            "(a Polylang language slug/locale, e.g. en or en_US). Never written as "
            "a target. Blank = 'en'.",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "source_category",
            "Source category id",
            "REQUIRED on Polylang FREE for "
            "whole-site translation: only posts in this category id are treated as "
            "sources. WordPress free exposes NO per-post language signal, so this "
            "category is the ONLY language fence - YOU must keep it source-language-"
            "only (the connector cannot verify a post's language on free). Without a "
            "scope the connector refuses whole-site translation. Ignored on Polylang "
            "Pro (the language filter is used). Leave blank if you use a source tag or "
            "pass explicit post ids.",
            required=False,
            placeholder="12",
        ),
        FieldSpec(
            "source_tag",
            "Source tag id",
            "Alternative to Source category id for "
            "the same purpose (Polylang FREE scope). Only posts with this tag id are "
            "treated as sources. Ignored on Polylang Pro.",
            required=False,
            placeholder="34",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated locales to create "
            "(e.g. fr, de, es). Each should exist as a language in Polylang for the "
            "translation link to attach.",
            required=False,
            placeholder="fr, de, es",
        ),
        FieldSpec(
            "source_status",
            "Source status",
            "Which source statuses to translate: "
            "publish (default), draft, pending, private, future, or any. Sent "
            "explicitly on discovery - never the REST publish-only default.",
            required=False,
            placeholder="publish",
        ),
        FieldSpec(
            "publish",
            "Publish translations",
            "1 = create/keep target posts "
            "PUBLISHED; 0 (default) = create as DRAFTS for review. Applies to both "
            "posts and pages (the status is always sent explicitly).",
            required=False,
            placeholder="0",
        ),
        FieldSpec(
            "max_articles",
            "Max posts",
            "Safety cap on source posts scanned per run.",
            required=False,
            placeholder="50",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "username",
            "Username",
            "The WordPress username the Application Password "
            "belongs to (its author/editor role gates what the connector can do). "
            "Sent as the HTTP Basic username.",
            secret=True,
            placeholder="editor",
        ),
        FieldSpec(
            "application_password",
            "Application Password",
            "A WordPress Application "
            "Password (wp-admin -> Users -> Profile -> Application Passwords). NOT "
            "your login password. Sent as the HTTP Basic password.",
            secret=True,
            placeholder="xxxx xxxx xxxx xxxx xxxx xxxx",
        ),
    ],
    setup_steps=[
        "In WordPress, install and activate Polylang (free is enough to connect and to "
        "list languages; Polylang PRO adds the corroborating translation link - see the "
        "runbook). Add each target language under Languages.",
        "Create an Application Password: wp-admin -> Users -> Profile -> Application "
        "Passwords, for a user with at least Editor role (Editor can publish; Author "
        "cannot publish others' posts). Copy the generated password.",
        "Enter the Site URL (your site root, e.g. https://blog.acme.com), the username the "
        "Application Password belongs to, and the Application Password.",
        "Set the post types (posts and/or pages), your source locale, and target locales.",
        "WordPress has no content webhook, so run on a schedule (Every N minutes) - the "
        "source-content hash keeps translations fresh.",
    ],
    other_side="WordPress change detection polls the configured post types and gates "
    "re-translation on a per-(post, locale) source-content hash (modified_gmt "
    "bumps on any touch, so it never drives a watermark). Each translation is a "
    "separate post in the target language, tracked in the connector's id map; a "
    "pre-existing linked translation (Polylang Pro) is never overwritten "
    "(needs_link). Raw block content is read and written (context=edit) so "
    "Gutenberg blocks and shortcodes are preserved. Without Polylang Pro the "
    "connector runs on its own id map with no source-side corroboration.",
    docs_url="https://developer.wordpress.org/rest-api/reference/posts/",
    supports_oauth=False,
)


class WordPressConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="wordpress",
        topology=Topology.T3,  # THIRD T3 consumer (document-per-locale)
        auth=(
            AuthKind.BASIC,
        ),  # Application Passwords over HTTP Basic (drift fix: was API_KEY; nothing branches on capability.auth)  # noqa: E501
        api_version=_WP_NS,
        rich_text=RichText.HTML,  # raw block content routes through the html filter
        regions="base_url",
        change_signals=ChangeSignals(
            webhook_sig=None,  # WP core has NO content webhook -> poll-only
            poll_field="modified_gmt",  # content signal, but metadata-tainted -> NO watermark
            stale_flag=None,
            thin_payload=True,
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        # IDENTICAL T3 wiring to Notion/Help Scout - reserve-then-fill + 7-case
        # reconciliation live entirely in the vendor-neutral DocPerLocaleStrategy + idmap.
        self.strategy = DocPerLocaleStrategy(
            source_locale_of=lambda conn, rec: self._source_locale()
        )
        self._languages_cache = None  # list[dict] | None (None = not probed yet)
        self._mode_cache = None  # str | None (Polylang mode, cached per instance)
        # Per-run operator-visible accumulators (surfaced by build_message / build_result).
        self._superseded: list = []  # target refs whose human edits we overwrote (M2)
        self._named_own_skipped: list = []  # explicit ids that were our own targets (M4)
        self._img_alt_total: int = 0  # cover-block image alt/title units NOT translated (M5)
        self._shortcode_masked_total: int = 0  # shortcode spans masked (round-2 M1 report)
        self._shortcode_lost_total: int = 0  # pairs HELD for a lost shortcode (round-2 H2)
        self._written_delta: dict = {}  # target_ref -> hash of what we last wrote (M2)
        self._run_source_refs: set = (
            set()
        )  # refs discovered as SOURCES this run (excl. from reclaim)
        self._src_title_counts: dict = {}  # source-title multiset (reclaim uniqueness gate)
        self._explicit_on_free: bool = False  # explicit ids used on a free tier (M3 note)
        self._cached_client = None  # reused across target_health probes per run (L1)

    # ---------------------------------------------------------- config helpers
    def _source_locale(self) -> str:
        return (self.config.get("source_locale") or "en").strip().lower() or "en"

    def _publish(self) -> bool:
        return str(self.config.get("publish", "0")).strip().lower() in ("1", "true", "yes")

    def _write_status(self) -> str:
        """The explicit status EVERY create/update sends - posts AND pages (gotcha #3)."""
        return "publish" if self._publish() else "draft"

    def _source_status(self) -> str:
        s = (self.config.get("source_status") or "publish").strip().lower() or "publish"
        if s not in _SOURCE_STATUS_SET:
            raise ConnectorError(
                f"Invalid source_status {s!r}; use one of {', '.join(sorted(_SOURCE_STATUS_SET))}."
            )
        return s

    def _scope_params(self) -> dict:
        """B1: the explicit discovery SCOPE on Polylang FREE (a category or tag id) that
        confines auto-discovery so existing OTHER-LANGUAGE posts are never treated as
        sources. Returns the WP REST filter params ({'categories': id} / {'tags': id}), or
        {} when no scope is configured. Validated to bare integers (taxonomy A)."""
        out: dict = {}
        cat = (self.config.get("source_category") or "").strip()
        tag = (self.config.get("source_tag") or "").strip()
        if cat:
            if not _ID_RE.match(cat):
                raise ConnectorError(f"Invalid source_category id {cat!r} (must be an integer).")
            out["categories"] = cat
        if tag:
            if not _ID_RE.match(tag):
                raise ConnectorError(f"Invalid source_tag id {tag!r} (must be an integer).")
            out["tags"] = tag
        return out

    def _has_scope(self) -> bool:
        return bool(self._scope_params())

    def _post_types(self) -> list[str]:
        raw = (self.config.get("post_types") or "").strip()
        if not raw:
            return ["posts"]
        out: list[str] = []
        for x in raw.replace(";", ",").replace("\n", ",").split(","):
            t = x.strip().lower()
            if not t:
                continue
            # Tolerate the singular ('post'/'page') by normalizing to the REST plural.
            if t in ("post", "page"):
                t = t + "s"
            if t not in _SUPPORTED_SET:
                raise ConnectorError(
                    f"Unsupported WordPress post type {x.strip()!r}; supported: "
                    f"{', '.join(_SUPPORTED_TYPES)}."
                )
            if t not in out:
                out.append(t)
        if not out:
            raise ConnectorError("At least one post type is required (posts and/or pages).")
        return out

    # ------------------------------------------------------------ id / ref shapes
    def _split_ref(self, ref: str, types: list[str] | None = None) -> tuple[str, str]:
        """Parse + SCOPE-VALIDATE a '<type>:<numeric>' ref (taxonomy A, fail-closed). The
        type MUST be a WordPress type this connection is configured for, and the id MUST be
        a bare positive integer (no '/','?','#','..', no whitespace) - so a caller-supplied
        ref can never escape the /wp-json/wp/v2/<type>/ path or target an out-of-scope
        type. A bare numeric id is accepted only for a single-type connection."""
        types = types if types is not None else self._post_types()
        r = str(ref or "").strip()
        if ":" in r:
            t, _sep, num = r.partition(":")
            t = t.strip().lower()
            if t in ("post", "page"):
                t = t + "s"
            if t not in _SUPPORTED_SET:
                raise ConnectorError(f"Unknown WordPress type in ref {ref!r}.")
            if t not in types:
                raise ConnectorError(
                    f"Type {t!r} in ref {ref!r} is not configured on this connection "
                    f"({', '.join(types)}) - refusing to target it."
                )
            num = num.strip()
        else:
            if len(types) != 1:
                raise ConnectorError(
                    f"Ambiguous ref {ref!r}: this connection has multiple post types, so "
                    f"refs must be qualified as '<type>:<id>' (e.g. 'posts:12')."
                )
            t, num = types[0], r
        if not _ID_RE.match(num):
            raise ConnectorError(
                f"Unsafe post id {num!r} in ref {ref!r}: must be a bare positive integer."
            )
        return t, num

    @staticmethod
    def _ref(post_type: str, post_id) -> str:
        return f"{post_type}:{post_id}"

    # -------------------------------------------------------------- auth / http
    def _base(self) -> str:
        raw = (self.config.get("base_url") or "").strip()
        if not raw:
            raise ConnectorError(
                "WordPress Site URL is required (your site root, e.g. https://blog.acme.com)."
            )
        root = _validate_target(raw).rstrip("/")
        if root.lower().endswith("/wp-json"):
            root = root[: -len("/wp-json")].rstrip("/")
        return f"{root}/wp-json"

    def _headers(self, *, write: bool = False) -> dict:
        # Application Passwords: HTTP Basic where password = the app token. WP core reads
        # the header as ISO-8859-1; app passwords are ASCII and usernames are typically
        # ASCII, so UTF-8 encoding is byte-identical for those (a non-ASCII username is a
        # documented edge - see the runbook). Built by hand so nothing logs the token.
        user = (self.secret.get("username") or "").strip()
        pw = (self.secret.get("application_password") or "").strip()
        if not user or not pw:
            raise ConnectorError("WordPress username and Application Password are required.")
        token = base64.b64encode(f"{user}:{pw}".encode()).decode("ascii")
        h = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        if write:
            h["Content-Type"] = "application/json"
        return h

    def _wp_url(self, *segments, ns: str = _WP_NS) -> str:
        from urllib.parse import quote

        tail = "/".join(quote(str(s), safe="") for s in segments)
        return f"{self._base()}/{ns}/{tail}" if tail else f"{self._base()}/{ns}"

    def _get(self, c, url, **kw):
        return self._request(c, "GET", url, headers=self._headers(), **kw)

    # ---------------------------------------------------------------- languages
    def _fetch_languages(self):
        """Polylang's languages (the one FREE Polylang REST surface, bundled since 3.7).
        Returns a list of language dicts, or None when Polylang is NOT installed (the
        pll/v1 route 404s / 'no route'). Cached per instance."""
        if self._languages_cache is not None:
            return self._languages_cache
        try:
            with self._client() as c:
                r = self._get(c, self._wp_url("languages", ns=_PLL_NS))
        except httpx.TransportError as e:
            raise ConnectorError(f"WordPress unreachable: {type(e).__name__}: {e}") from e
        if r.status_code == 404:
            self._languages_cache = None
            return None  # Polylang absent (no pll/v1 route)
        if r.status_code in (401, 403):
            raise ConnectorError(
                f"WordPress rejected the credentials ({r.status_code}) - check the username "
                f"and Application Password (and that the user may use the REST API)."
            )
        if r.status_code != 200:
            # A 'rest_no_route' body also means Polylang is not providing pll/v1.
            if "rest_no_route" in (r.text or ""):
                self._languages_cache = None
                return None
            raise ConnectorError(
                f"WordPress pll/v1/languages -> {r.status_code}: {vendor_excerpt(r.text)}"
            )
        body = r.json()
        langs = body if isinstance(body, list) else (body.get("languages") or [])
        out = [x for x in langs if isinstance(x, dict) and (x.get("slug") or x.get("locale"))]
        self._languages_cache = out
        return out

    def enabled_locales(self) -> list[str]:
        """Polylang language slugs (lowercased) when Polylang is present; [] when absent
        (the flow's preflight then treats every configured target as writable - a T3
        connector creates NEW posts, so any locale is a valid new-post language)."""
        langs = self._fetch_languages()
        if not langs:
            return []
        out: list[str] = []
        for x in langs:
            for key in ("slug", "locale"):
                v = str(x.get(key) or "").strip().lower()
                if v and v not in out:
                    out.append(v)
        return out

    def _lang_slug(self, locale: str) -> str:
        """Map an internal locale to Polylang's language SLUG (what `?lang=` /
        `?translations[xx]=` expect). Matches the languages list by slug OR locale
        (case-insensitive); falls back to the given locale string when Polylang is absent
        or the locale is not a known language (best-effort - the link just won't attach,
        surfaced as an operator note; live QA confirms the real behavior)."""
        want = str(locale or "").strip().lower()
        for x in self._fetch_languages() or []:
            slug = str(x.get("slug") or "").strip()
            loc = str(x.get("locale") or "").strip()
            if want in (slug.lower(), loc.lower()):
                return slug or want
        return want

    def unknown_target_locales(self, targets) -> set:
        """Configured target locales that are NOT known Polylang languages - surfaced as an
        operator note (like Shopify's unpublished-locale note): the translation post is
        still created, but the Polylang link may not attach until the language is added
        under Languages. Empty when Polylang is absent (nothing to check against)."""
        langs = self._fetch_languages()
        if not langs:
            return set()
        known = set(self.enabled_locales())
        return {l for l in (targets or []) if str(l).strip().lower() not in known}  # noqa: E741

    # ------------------------------------------------------ Polylang mode (gotcha #2)
    def _polylang_mode(self) -> str:
        """Detect the Polylang tier ourselves (the reference has NO such check, digest
        §6.2). "pro" only when a real post's edit-context response carries a `translations`
        field - the Pro-gated linkage field. Probe order:
          * pll/v1/languages 404 / no-route -> ABSENT (Polylang not installed).
          * else fetch ONE post (any configured type) with context=edit; if its response
            carries a `translations` dict -> PRO; if not -> FREE.
          * no post exists to probe -> UNKNOWN (treated as pure T3, surfaced honestly).
        Cached per instance; never crashes on a null/absent translations field."""
        if self._mode_cache is not None:
            return self._mode_cache
        langs = self._fetch_languages()
        if langs is None:
            self._mode_cache = _MODE_ABSENT
            return self._mode_cache
        # Probe a single post per configured type for a translations field.
        mode = _MODE_UNKNOWN
        try:
            with self._client() as c:
                for t in self._post_types():
                    r = self._get(
                        c,
                        self._wp_url(t),
                        params={"context": "edit", "per_page": 1, "status": "any"},
                    )
                    if r.status_code != 200:
                        continue
                    items = r.json()
                    if not isinstance(items, list) or not items:
                        continue
                    sample = items[0]
                    if isinstance(sample, dict) and isinstance(sample.get("translations"), dict):
                        mode = _MODE_PRO
                    else:
                        mode = _MODE_FREE
                    break
        except (httpx.TransportError, ConnectorError, ValueError):
            # A probe failure is not fatal to the run; leave it UNKNOWN (pure T3).
            mode = _MODE_UNKNOWN
        self._mode_cache = mode
        return mode

    def _assisted(self) -> bool:
        """True only when Polylang PRO is detected - the sole mode where we read the
        `translations` map + write the `?lang=`/`?translations[xx]=` link."""
        return self._polylang_mode() == _MODE_PRO

    @staticmethod
    def mode_label(mode: str) -> str:
        return {
            _MODE_PRO: "Polylang Pro detected - assisted linkage (translations map "
            "corroborates our id map).",
            _MODE_FREE: "Polylang Pro NOT detected - degraded to uncorroborated tracking "
            "(our id map is the only source of truth).",
            _MODE_ABSENT: "Polylang not detected - uncorroborated tracking (our id map is "
            "the only source of truth); target-language linking is unavailable.",
            _MODE_UNKNOWN: "Polylang tier undetermined (no content to probe) - running "
            "uncorroborated (our id map is the only source of truth).",
        }.get(mode, mode)

    def persist_mode(self) -> str:
        """Detect + persist the Polylang mode on config so the connection page renders it
        WITHOUT a live call (digest §6.2 - operator-visible, not a log line). Called by the
        run preflight and the test path. Best-effort: a persistence failure never breaks a
        run."""
        mode = self._polylang_mode()
        if self.connection_id is not None:
            try:
                from app.services.connectors import store

                if (self.config.get("wordpress_polylang_mode") or "") != mode:
                    store.patch_config(self.connection_id, {"wordpress_polylang_mode": mode})
                    self.config["wordpress_polylang_mode"] = mode
            except Exception:  # noqa: S110 -- mode cache persist; it is re-detected next run
                pass
        return mode

    # ------------------------------------------------------------- test / list
    def test(self) -> tuple[bool, str]:
        try:
            langs = self._fetch_languages()
            mode = self.persist_mode()
            if langs is None:
                return True, (
                    f"Connected to WordPress. {self.mode_label(mode)} "
                    f"Install Polylang and add languages to enable the "
                    f"translation link."
                )
            return True, (
                f"Connected to WordPress ({len(langs)} Polylang language(s)). "
                f"{self.mode_label(mode)}"
            )
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        try:
            return [{"id": t, "name": f"{t} (post type)", "tables": []} for t in self._post_types()]
        except Exception:
            return []

    # --------------------------------------------------------- record normalize
    def _normalize(self, item: dict, post_type: str) -> dict | None:
        """A source post dict -> our record shape, reading RAW title/content (gotcha #1).
        Returns None (a skip) when content.raw is unavailable (edit context absent) -
        the caller records a loud per-post skip rather than translating rendered HTML."""
        if not isinstance(item, dict) or item.get("id") is None:
            return None
        raw_title = _raw_of(item.get("title"))
        raw_content = _raw_of(item.get("content"))
        if raw_content is None or raw_title is None:
            return {"_raw_missing": True, "id": str(item.get("id")), "_type": post_type}
        raw_excerpt = _raw_of(item.get("excerpt"))
        return {
            "ref": self._ref(post_type, item.get("id")),
            "_type": post_type,
            "id": str(item.get("id")),
            "title": raw_title,
            "content": raw_content,
            "excerpt": raw_excerpt if isinstance(raw_excerpt, str) else "",
            "slug": str(item.get("slug") or ""),
            "status": item.get("status") or "",
            "lang": item.get("lang") or "",
            "translations": item.get("translations")
            if isinstance(item.get("translations"), dict)
            else {},
            "modified_gmt": item.get("modified_gmt") or item.get("modified") or "",
        }

    # ------------------------------------------------------- pagination (gotcha #4)
    def _paginate(self, c, post_type: str, params: dict, cap: int) -> list[dict]:
        """List a post type with CORRECT pagination (digest §6.5 - the reference drops the
        last page). Loop `while page <= total_pages` off the X-WP-TotalPages header
        (case-insensitive), bounded by `cap`. Every param (status, lang, context=edit) is
        set by the caller."""
        out: list[dict] = []
        page = 1
        total_pages = None
        per_page = min(100, max(1, cap))
        offset_mode = False
        while len(out) < cap:
            p = dict(params)
            p["page"] = page
            p["per_page"] = per_page
            if offset_mode:
                p["offset"] = len(out)
            r = self._get_page(
                lambda **kw: self._get(c, self._wp_url(post_type), **kw),
                params=p,
                size_key="per_page",
            )
            if p["per_page"] != per_page:
                per_page = p["per_page"]
                offset_mode = True
                total_pages = None
            if r.status_code == 400 and "rest_post_invalid_page_number" in (r.text or ""):
                break  # asked past the last page (empty tail) -> done
            if r.status_code == 401 or r.status_code == 403:
                raise ConnectorError(
                    f"WordPress rejected the credentials listing {post_type} ({r.status_code})."
                )
            if r.status_code != 200:
                raise ConnectorError(
                    f"WordPress list {post_type} -> {r.status_code}: {vendor_excerpt(r.text)}"
                )
            items = r.json()
            if not isinstance(items, list):
                break
            out.extend(self._keep_rows(items))
            if total_pages is None:
                # Case-insensitive header match (the reference relied on exact case).
                tp = None
                for k, v in r.headers.items():
                    if k.lower() == "x-wp-totalpages":
                        tp = v
                        break
                try:
                    total_pages = int(tp) if tp is not None else 1
                except (TypeError, ValueError):
                    total_pages = 1
            if not items or (not offset_mode and page >= total_pages):
                break
            if not offset_mode:
                page += 1
        return out[:cap]

    def _own_target_refs(self) -> set:
        """Refs ('<type>:<id>') of posts we ourselves CREATED as targets (any id-map row's
        target for this connection). Excluded from discovery so a translation we made is
        never re-listed as a source to translate (pure-T3 steady state, works for both
        draft + published targets and both Pro + free)."""
        if self.connection_id is None:
            return set()
        try:
            return {
                str(row.target_id)
                for row in idmap.list_for_connection(self.connection_id)
                if row.target_id
            }
        except Exception:
            return set()

    def _fetch_post(self, c, post_type: str, post_id: str) -> dict | None:
        """One post by id with context=edit (so RAW title/content + Pro fields come back).
        Returns the RAW REST dict (not yet normalized), or None on 404."""
        r = self._get(c, self._wp_url(post_type, post_id), params={"context": "edit"})
        if r.status_code == 404:
            return None
        if r.status_code in (401, 403):
            raise ConnectorError(
                f"WordPress rejected the credentials reading {post_type}/{post_id} "
                f"({r.status_code})."
            )
        if r.status_code != 200:
            raise ConnectorError(
                f"WordPress get {post_type}/{post_id} -> {r.status_code}: {vendor_excerpt(r.text)}"
            )
        body = r.json()
        return body if isinstance(body, dict) else None

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source posts to translate. Explicit article_ids ('<type>:<id>' or a bare id for
        a single-type connection; the retry path) win and are SCOPE-VALIDATED (taxonomy A).
        Else each configured type is listed with an explicit status (never the REST
        publish-only default) and - with Polylang Pro - `lang=<source>` server-side; our
        OWN target posts are excluded (steady state). The shared cap is filled ROUND-ROBIN
        across types (taxonomy E) so the first type (posts) can never starve the others
        (pages) - each type gets a fair rotation of the budget, a short type spills its
        share to the rest. Correct pagination (gotcha #4)."""
        cap = fetch_cap(cap or self.config.get("max_articles"), 50)
        types = self._post_types()
        own = self._own_target_refs()
        with self._client() as c:
            if article_ids:
                out: list[dict] = []
                # M3: explicit ids on the FREE tier BYPASS the language fence - we cannot
                # verify a post's language on free, so a mis-named id could translate a
                # non-source post. Surface it as a per-run note (not a block - the operator
                # asked for these ids explicitly).
                if not self._assisted():
                    self._explicit_on_free = True
                for raw in list(article_ids)[:cap]:
                    t, num = self._split_ref(str(raw), types)
                    ref = self._ref(t, num)
                    # M4: an explicitly-named id that is one of OUR OWN targets is a
                    # translation, not a source - refuse it (report line), never re-translate.
                    if ref in own:
                        self._named_own_skipped.append(ref)
                        continue
                    item = self._fetch_post(c, t, num)
                    if item is not None:
                        rec = self._normalize(item, t)
                        if rec is not None:
                            out.append(self._charge_record(rec))
                return out
            # B1: on Polylang FREE there is NO per-post language signal, so translating the
            # whole site would treat existing OTHER-LANGUAGE posts as sources. Refuse
            # fail-closed unless the operator configures an explicit source scope (category /
            # tag); the id-map self-exclusion alone is NOT sufficient (it only removes posts
            # WE created). Pro is unrestricted (the lang=<source> filter is the signal).
            scope = self._scope_params()
            if not self._assisted() and not scope:
                raise ConnectorError(
                    "WordPress free (Polylang Pro not detected) exposes no per-post language "
                    "signal, so translating the whole site would treat existing other-"
                    "language posts as sources. Configure a Source category id or Source tag "
                    "id that contains ONLY source-language posts (the category/tag is the "
                    "ONLY language fence on this tier - the connector cannot verify a post's "
                    "language). Or pass explicit post ids (note: on the free tier explicit "
                    "ids also bypass the language fence - make sure they are source-language "
                    "posts). Polylang Pro removes this restriction."
                )
            params = {"context": "edit", "status": self._source_status()}
            if self._assisted():
                params["lang"] = self._lang_slug(self._source_locale())
            else:
                params.update(scope)  # FREE: confine discovery to the operator's scope
            per_type: list[list[dict]] = []
            for t in types:
                lst = []
                for item in self._paginate(c, t, dict(params), cap):
                    rec = self._normalize(item, t)
                    if rec is None or rec.get("ref") in own:
                        continue  # unreadable/rendered-only, or our own translation post
                    lst.append(rec)
                per_type.append(lst)
        records: list[dict] = []
        i = 0
        while len(records) < cap and any(i < len(lst) for lst in per_type):
            for lst in per_type:
                if i < len(lst):
                    records.append(lst[i])
                    if len(records) >= cap:
                        break
            i += 1
        return records[:cap]

    def show_post(self, ref: str) -> dict | None:
        t, num = self._split_ref(str(ref))
        with self._client() as c:
            item = self._fetch_post(c, t, num)
        return self._normalize(item, t) if item is not None else None

    # ------------------------------------------- write-back payload assembly
    @staticmethod
    def _has_translated_content(fields: dict) -> bool:
        """False when a write-back payload carries ZERO translated content (every segment
        refused/NEEDS_REVIEW this run - the flow stamps `_no_translation`). Creating a
        target post whose body is verbatim source text is a silent untranslated clone; the
        strategy fails the pair instead (Notion/Help Scout parity)."""
        return not bool((fields or {}).get("_no_translation"))

    def _write_body(self, fields: dict, *, create: bool) -> dict:
        """The create/update JSON body: raw title + raw content (+ excerpt). content/title
        are written RAW (gotcha #1) - WordPress stores the string verbatim as post_content,
        so Gutenberg block markers + masked shortcodes survive.

        STATUS is sent ONLY on CREATE (posts AND pages get an explicit status, gotcha #3). On
        UPDATE the status is OMITTED (M1) - WordPress's partial update keeps the target's
        current status, so re-translating a source never un-publishes a human-published
        translation."""
        body: dict = {
            "title": (fields or {}).get("title") or "",
            "content": (fields or {}).get("body") or "",
        }
        if "excerpt" in (fields or {}):
            body["excerpt"] = (fields or {}).get("excerpt") or ""
        if create:
            body["status"] = self._write_status()
            # Round-2 blocker: set the target's slug to the SOURCE slug so a crash-orphan
            # (create landed, reserve did not) is reclaimable by an exact `?slug=` query -
            # a stable identity key WE control, unlike an auto-derived translated slug we
            # cannot predict (Help Scout parity). Polylang localizes the slug later; live QA.
            src_slug = str((fields or {}).get("source_slug") or "").strip()
            if src_slug:
                body["slug"] = src_slug
        return body

    def _target_snapshot_hash(self, item: dict) -> str:
        """Content hash of a target post's CURRENT raw title/content/excerpt - for the M2
        human-edit-detection comparison. Uses _raw_of (edit context)."""
        return content_hash(
            _raw_of(item.get("title")) or "",
            _raw_of(item.get("content")) or "",
            _raw_of(item.get("excerpt")) or "",
        )

    # --- reused client for the per-run target_health probes (round-2 L1) ---
    def _run_client(self):
        """One httpx client reused across a run's target_health probes (L1: not a new client
        per suppressed pair). Closed by _close_run_client at end of run."""
        if self._cached_client is None:
            self._cached_client = self._client()
        return self._cached_client

    def _close_run_client(self) -> None:
        c = self._cached_client
        self._cached_client = None
        if c is not None:
            try:  # noqa: SIM105
                c.close()
            except Exception:  # noqa: S110 -- end-of-run HTTP client close; the run is over
                pass

    def target_health(self, target_ref: str) -> str:
        """H2 external-deletion detection: corroborate a `done` id-map row with a cheap live
        GET of the target id (one GET per suppressed pair per run - the honest cost of making
        the connection page's 'a deleted post is recreated' promise true; the client is reused
        across the run, L1). Returns:
          * 'gone'  - the target 404s OR is in the TRASH (round-2 M4: context=edit returns a
                      trashed post with 200, which must be treated as missing) -> recreate;
          * 'blank' - the target exists but its title AND content are both empty (a human
                      BLANKED it) -> refill the machine content (rule 2);
          * 'ok'    - the target exists with content -> honestly suppress.
        A human-EDITED (non-blank) target is 'ok' here; its human-edit honesty is M2's job. A
        probe failure (transport / non-200 / bad JSON, round-2 L3) returns 'ok' so a garbage
        response SUPPRESSES (safe) rather than triggering a destructive recreate."""
        try:
            t, num = self._split_ref(str(target_ref))
        except ConnectorError:
            return "gone"
        try:
            item = self._fetch_post(self._run_client(), t, num)
        except (httpx.TransportError, ConnectorError, ValueError):
            return "ok"  # a probe failure must NOT trigger a destructive recreate (L3)
        if item is None:
            return "gone"
        if str(item.get("status") or "").lower() == "trash":
            return "gone"  # M4: a trashed target is effectively missing -> recreate
        title = _raw_of(item.get("title")) or ""
        content = _raw_of(item.get("content")) or ""
        return "blank" if (not title.strip() and not content.strip()) else "ok"

    # ------------------------------ the four T3DocClient methods the strategy drives
    def create_target(self, source_id: str, locale: str, fields: dict):
        """RESERVE: create the target-language post and return its '<type>:<id>' ref.
        Returns (ref | None, error).

        With Polylang PRO the create carries `?lang=<target_slug>` +
        `?translations[<source_slug>]=<source_id>` so Polylang both tags the new post's
        language AND splices it into the source's translation group (the assisted link).
        Without Pro those params are omitted (they would be silently ignored on free / 400
        without Polylang) and the post is a plain new row tracked only by our id map. A
        fully-refused translation is NOT created (never an untranslated clone). The type is
        inferred from the SOURCE ref (a translation of a post is a post; of a page, a page)."""
        if not self._has_translated_content(fields):
            return None, "all segments refused/flagged - post not created"
        if (fields or {}).get("_shortcode_lost"):  # round-2 H2: hold, never write broken
            self._shortcode_lost_total += 1
            return None, (
                "shortcode integrity check failed - a masked shortcode was lost or a "
                "shortcode was added, changed, or duplicated; held (not written), will re-attempt"
            )
        try:
            t, src_num = self._split_ref(str(source_id))
        except ConnectorError as e:
            return None, str(e)
        params = {"context": "edit"}
        if self._assisted():
            params["lang"] = self._lang_slug(locale)
            src_slug = self._lang_slug(self._source_locale())
            params[f"translations[{src_slug}]"] = src_num
        try:
            with self._client() as c:
                # POST is non-idempotent (create) -> never resend on 5xx/timeout; the id
                # map + reserve lock + the find_preexisting orphan reclaim guard duplicate
                # risk, not a blind retry.
                r = self._request(
                    c,
                    "POST",
                    self._wp_url(t),
                    params=params,
                    idempotent=False,
                    headers=self._headers(write=True),
                    json=self._write_body(fields, create=True),
                )
        except httpx.TransportError as e:
            return None, f"{type(e).__name__}: {e}"
        if r.status_code in (200, 201):
            body = r.json() if r.content else {}
            new_id = body.get("id") if isinstance(body, dict) else None
            return (self._ref(t, new_id), None) if new_id else (None, "create returned no id")
        return None, f"create {t} -> {r.status_code}: {vendor_excerpt(r.text)}"

    def fill_target(self, target_id: str, fields: dict) -> WriteResult:
        """Write/replace the target post's content by id (PUT - idempotent, so a resumed
        build or an update re-runs it safely). On an UPDATE whose retranslation collapsed to
        zero content, bail BEFORE the PUT so a previously-good translation is left untouched
        (never clobbered by source text). content/title RAW (gotcha #1); status OMITTED so a
        human-published target is never un-published (M1).

        M2 HONESTY: before overwriting, read the target's CURRENT content and compare it to
        the hash of what WE last wrote. If they differ, a human edited the target since our
        last write - we still overwrite (re-translation on source change is the design), but
        the pair is recorded as SUPERSEDED so the run message/result says so per pair (visible,
        not silent). Round-2 fix: we persist the hash of the PUT RESPONSE (what the server
        actually STORED), not the bytes we SENT - so server-side normalization (kses,
        entities, whitespace) does not make every later update falsely report a human edit."""
        if not self._has_translated_content(fields):
            return WriteResult(
                False,
                False,
                target_id=str(target_id),
                message="all segments refused/flagged - post not written",
            )
        if (fields or {}).get("_shortcode_lost"):  # round-2 H2: hold, never write broken
            self._shortcode_lost_total += 1
            return WriteResult(
                False,
                False,
                target_id=str(target_id),
                message="shortcode integrity check failed - a masked shortcode "
                "was lost or a shortcode was added, changed, or duplicated; held (not written)",
            )
        try:
            t, num = self._split_ref(str(target_id))
        except ConnectorError as e:
            return WriteResult(False, False, target_id=str(target_id), message=str(e))
        ref = self._ref(t, num)
        body = self._write_body(fields, create=False)
        prior = (self.config.get("wordpress_written") or {}).get(ref)
        try:
            with self._client() as c:
                # M2: only read the current target when we have a PRIOR write to compare
                # against (a fresh create has nothing to supersede) - keeps the extra GET off
                # the create path.
                cur = self._fetch_post(c, t, num) if prior else None
                r = self._request(
                    c,
                    "PUT",
                    self._wp_url(t, num),
                    params={"context": "edit"},
                    idempotent=True,
                    headers=self._headers(write=True),
                    json=body,
                )
        except httpx.TransportError as e:
            return WriteResult(
                False, False, target_id=str(target_id), message=f"{type(e).__name__}: {e}"
            )
        if r.status_code in (200, 201):
            # M2: a human edited the target since our last write -> record superseded.
            if prior and isinstance(cur, dict) and self._target_snapshot_hash(cur) != prior:
                self._superseded.append(ref)
            # Persist the hash of what the SERVER STORED (the PUT response under context=edit),
            # so the next update's comparison is normalized-vs-normalized (round-2 H3).
            try:
                stored = self._target_snapshot_hash(r.json())
            except (ValueError, TypeError):
                stored = content_hash(
                    body.get("title") or "", body.get("content") or "", body.get("excerpt") or ""
                )
            self._persist_written(ref, stored)
            return WriteResult(True, False, target_id=str(target_id), message="filled")
        return WriteResult(
            False,
            False,
            target_id=str(target_id),
            message=f"update {t}/{num} -> {r.status_code}: {vendor_excerpt(r.text)}",
        )

    def _persist_written(self, ref: str, written_hash: str) -> None:
        """Record the hash of what we just wrote to a target (M2), both in the per-run cache
        (so a same-run second write sees it) and persisted to config `wordpress_written`."""
        self._written_delta[ref] = written_hash
        w = dict(self.config.get("wordpress_written") or {})
        w[ref] = written_hash
        self.config["wordpress_written"] = w
        if self.connection_id is not None:
            try:
                from app.services.connectors import store

                store.patch_config_nested(
                    self.connection_id, {"wordpress_written": {"add": {ref: written_hash}}}
                )
            except Exception:
                # The persisted hash guards human edits on the target (M2); a lost write
                # weakens that guard on the next run, so surface it.
                log.warning(
                    "wordpress connection %s: could not persist written hash for %s",
                    self.connection_id,
                    ref,
                    exc_info=True,
                )

    def verify_target(self, target_id: str) -> bool:
        """Does the target post STILL EXIST (not deleted/404/trashed)? A fetch of the id
        with context=edit (SPEC-t3 rule: verify = fetch the id, never a title/content
        match). context=edit returns the post regardless of draft/publish status, so a
        draft target we created is always findable (the H4 fix, gotcha #3). Round-2 M4: a
        TRASHED post also returns 200 under context=edit - treat it as NOT present so the
        reconciliation recreates it (matching target_health), never updates a trashed post."""
        try:
            t, num = self._split_ref(str(target_id))
        except ConnectorError:
            return False
        try:
            with self._client() as c:
                r = self._get(c, self._wp_url(t, num), params={"context": "edit"})
        except (httpx.TransportError, ConnectorError) as e:
            # Issue #444: a timeout / dropped connection says nothing about the post.
            raise TargetUnverifiable(f"{type(e).__name__}: {e}") from e
        if r.status_code in (404, 410):
            return False  # the ONLY definite "gone" answer (rest_post_invalid_id)
        if r.status_code != 200:
            # 503 / 429 / 401 / 403 ...: could not verify. Never "deleted": the strategy
            # holds the pair for retry instead of orphaning the live post + duplicating it.
            raise TargetUnverifiable(f"HTTP {r.status_code} verifying {target_id}")
        try:
            status = str((r.json() or {}).get("status") or "").lower()
        except (ValueError, TypeError):
            return True  # 200 but unparseable -> conservatively 'present' (no destructive recreate)
        return status != "trash"

    def find_preexisting(self, source_id: str, locale: str, fields: dict):
        """RULE-6 / ORPHAN-RECLAMATION collision probe. Returns a pre-existing target's
        '<type>:<id>' ref (-> the strategy fails closed, needs_link, never overwriting it) or
        None. The reclamation window is the crash between create_target's POST and
        idmap.reserve, and the delete-and-re-add-connection case (id map wiped, targets still
        live) - without this the connector would DUPLICATE + relay-translate every run.

        Round-2 blocker fix - the probe is now DISAMBIGUATED so it can never mis-attach an
        UNRELATED post (two same-titled sources; a cross-locale MT-unchanged brand title):
          * CANDIDATES EXCLUDE: the source itself, every ref discovered as a SOURCE this run,
            and every one of OUR OWN targets (id-map). So a match is only ever a post that is
            neither a source nor a target we already track.
          * PRIMARY KEY = the WP `slug` (server-side `?slug=`): our crash-orphan carries the
            SOURCE slug (we set it on create), so `?slug=<source_slug>` finds it exactly.
          * On PRO the search is LANGUAGE-SCOPED (`lang=<target_slug>`) and the source's
            `translations` map is consulted first (a reliable server-owned link).
          * TITLE is a FALLBACK, used ONLY when the source title is UNIQUE among this run's
            sources (Help Scout's non-unique-fallback precedent) - so two "FAQ" sources never
            bind to each other, and a short MT-unchanged brand title never binds a later
            locale to the source. Matched against our minted title AND the source title.

        A returned ref is a POSSIBLE match: the strategy records needs_link and the connection
        page shows it as a match to REVIEW (never asserts it IS the translation).

        DOCUMENTED RESIDUAL: a human translation with a DIFFERENT slug and a localized title
        (and, on free, no link) shares no key we probe -> None -> a separate target is minted
        beside it. The human's post is NEVER touched (we only write our own map's id), so the
        failure mode is a duplicate, not a clobber."""
        try:
            t, src_num = self._split_ref(str(source_id))
        except ConnectorError:
            return None
        src_ref = self._ref(t, src_num)
        exclude = set(self._own_target_refs()) | set(self._run_source_refs) | {src_ref}
        source_slug = str((fields or {}).get("source_slug") or "").strip()
        minted = str((fields or {}).get("title") or "").strip()
        source_title = str((fields or {}).get("source_title") or "").strip()
        title_unique = bool((fields or {}).get("source_title_unique"))
        want_titles = {minted}
        if title_unique and source_title:
            want_titles.add(source_title)
        want_titles.discard("")
        target_slug = self._lang_slug(locale).strip().lower()
        cap = min(50, max(1, to_int(self.config.get("max_articles"), 50)))

        def _pick(items):
            for cand in items or []:
                if not isinstance(cand, dict):
                    continue
                cref = self._ref(t, cand.get("id"))
                if cref in exclude:
                    continue  # never a source or one of our own targets
                yield cand, cref

        try:
            with self._client() as c:
                # (a) PRO: the source's translations map (the reliable server link) first.
                if self._assisted():
                    item = self._fetch_post(c, t, src_num)
                    trans = item.get("translations") if isinstance(item, dict) else None
                    if isinstance(trans, dict):  # null map -> skip (never crash, gotcha #2)
                        for k, v in trans.items():
                            if str(k).strip().lower() == target_slug and v:
                                cref = self._ref(t, v)
                                if cref not in exclude and isinstance(
                                    self._fetch_post(c, t, str(v)), dict
                                ):
                                    return cref
                # (b) PRIMARY: slug match (language-scoped on Pro).
                if source_slug:
                    params = {
                        "context": "edit",
                        "status": "any",
                        "slug": source_slug,
                        "per_page": cap,
                    }
                    if self._assisted():
                        params["lang"] = target_slug
                    r = self._get(c, self._wp_url(t), params=params)
                    if r.status_code == 200:
                        for cand, cref in _pick(r.json()):
                            if str(cand.get("slug") or "").strip().lower() == source_slug.lower():
                                return cref
                # (c) FALLBACK: title match, gated on source-title uniqueness in the run.
                for term in list(want_titles):
                    params = {"context": "edit", "status": "any", "search": term, "per_page": cap}
                    if self._assisted():
                        params["lang"] = target_slug
                    r = self._get(c, self._wp_url(t), params=params)
                    if r.status_code != 200:
                        continue
                    for cand, cref in _pick(r.json()):
                        if (_raw_of(cand.get("title")) or "").strip() in want_titles:
                            return cref
        except (httpx.TransportError, ConnectorError, ValueError):
            return None
        return None

    # ------------------------------------------------- generic Connector surface
    def pull(self) -> list[PulledItem]:
        """One PulledItem per (post, unit) for the generic pull view. The rich flow is
        store.wordpress_translate (creates a per-locale post per source post)."""
        from app.filters import get_filter

        out: list[PulledItem] = []
        for rec in self.discover_articles():
            if rec.get("_raw_missing"):
                continue
            ref = rec["ref"]
            title = rec.get("title") or ""
            out.append(PulledItem(external_id=ref, field="title:0", text=title, title=title[:60]))
            body = rec.get("content") or ""
            for i, u in enumerate(get_filter("html").parse(body.encode("utf-8")).units):
                out.append(
                    PulledItem(external_id=ref, field=f"body:{i}", text=u.text, title=title[:60])
                )
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError(
            "WordPress creates a per-locale post per source post - use 'Translate now'."
        )

    def detect_change(self, conn=None, watermark: int = 0):
        """Poll-only change detection: re-list the configured post types for the caller to
        diff against the persisted per-(post, locale) content hash. Returns (posts, 0) -
        modified_gmt is metadata-tainted, so the hash in the flow is the gate; NO watermark
        is advanced."""
        return self.discover_articles(), 0


# ===========================================================================
# Translate flow - thin hooks over the shared engine (flow.run_translate_flow).
# T3 (document-per-locale): each translation is a SEPARATE post/page created in the
# target language and tracked in ConnectorIdMap via DocPerLocaleStrategy (the SAME
# reserve-then-fill + 7-case reconciliation Notion/Help Scout built). Body is raw block
# HTML -> get_filter("html") (which preserves Gutenberg block comments as skeleton).
# Poll-first, so NO watermark (modified_gmt is metadata-tainted - the content hash is
# the gate).
# ===========================================================================
def _wp_preflight(ctx):
    # M3: detect the Polylang mode FRESH and thread it through the run context (ctx.extra),
    # NOT the ctx.cfg snapshot (which is one run stale - it predates this run's probe). Also
    # persist it so the connection page can render it without a live call (gotcha #2). enabled
    # = every target locale EXCEPT the source: a NEW-post T3 connector can create a post in
    # any language; a target not yet added in Polylang is surfaced as a note, not a block.
    mode = ctx.connector.persist_mode()
    src = (ctx.cfg.get("source_locale") or "en").strip().lower()
    enabled = {l for l in ctx.locales if l and l != src}  # noqa: E741
    return enabled, {"mode": mode}


def _wp_prepare(ctx, art):
    from app.filters import get_filter

    if art.get("_raw_missing"):
        # gotcha #1: only rendered content was available (the user lacks edit capability),
        # so we REFUSE to translate it (rendered HTML would flatten Gutenberg blocks / bake
        # in shortcodes). A loud per-post skip with a clear error, never a silent flatten.
        rid = f"{art.get('_type')}:{art.get('id')}"
        return flow.SkipArticle(
            "raw_unavailable_skipped",
            error=f"{rid}: content.raw unavailable (context=edit did not return raw - the "
            f"connection's user likely lacks edit capability); refusing to translate "
            f"rendered HTML (would flatten Gutenberg blocks).",
        )
    ref = art["ref"]
    title = art.get("title") or ""
    raw_body = art.get("content") or ""
    excerpt = art.get("excerpt") or ""
    if not title.strip() and not raw_body.strip() and not excerpt.strip():
        return flow.SkipArticle("empty_skipped")
    conn = ctx.connector
    # Round-2 blocker: remember this run's SOURCE refs + the source-title multiset ONCE, so
    # find_preexisting can exclude sources and gate its title fallback on uniqueness (Help
    # Scout's _hs_src_title_counts precedent). Computed lazily from ctx.articles.
    if not getattr(conn, "_run_prepared", False):
        conn._run_prepared = True
        for a in ctx.articles or []:
            if a.get("ref"):
                conn._run_source_refs.add(a["ref"])
            nm = (a.get("title") or "").strip().lower()
            if nm:
                conn._src_title_counts[nm] = conn._src_title_counts.get(nm, 0) + 1
    title_unique = (not title.strip()) or conn._src_title_counts.get(title.strip().lower(), 0) <= 1
    # H3: MASK shortcodes in TEXT NODES before segmentation so `[contact-form-7 ...]` (and its
    # attribute values) is NEVER sent to the engine; it round-trips verbatim. The html filter
    # treats the mask sentinel (an HTML comment) as skeleton. Both parse (here) and build use
    # the SAME masked bytes so unit indices align.
    masked_body, shortcodes = _mask_shortcodes(raw_body)
    conn._shortcode_masked_total += len(shortcodes)  # round-2 M1: report masked span count
    body_units = []
    if excerpt.strip():  # M5: excerpt is a first-class translatable field (p_index 'excerpt')
        body_units.append(_Unit(excerpt, "excerpt"))
    # M5: image alt text IS translated (html filter a11y units), except inside a Gutenberg
    # cover block, whose JSON keeps its own alt copy: those stay source and are counted.
    cover_skip = _cover_image_units(masked_body.encode("utf-8"))
    conn._img_alt_total += len(cover_skip)
    body_units += [
        u
        for u in get_filter("html").parse(masked_body.encode("utf-8")).units
        if u.p_index not in cover_skip
    ]
    # Hash over the ORIGINAL raw title/body/excerpt (a change to any re-translates; the mask
    # is deterministic so masking or not would not change WHICH source re-runs).
    chash = content_hash(title, raw_body, excerpt)
    return flow.ArticlePrep(
        aid=ref,
        chash=chash,
        title_text=title,
        body_units=body_units,
        extra_skeleton={"article_id": ref, "title": title},
        doc_title=f"{(title or art.get('_type') or 'post')[:50]} ({ref})",
        extra={
            "masked_body_bytes": masked_body.encode("utf-8"),
            "shortcodes": shortcodes,
            "source_title": title,
            "source_excerpt": excerpt,
            "source_slug": art.get("slug") or "",
            "source_title_unique": title_unique,
            "has_excerpt": bool(excerpt.strip()),
            "post_type": art.get("_type"),
        },
    )


def _wp_is_suppressed(connector, aid, loc):
    # H2: a `done` id-map row is NOT trusted blindly. First the MAP check (SPEC-t3 invariant:
    # exists_target reads the map, never content); THEN a cheap live GET corroborates the
    # target still exists and is not human-blanked (one GET per suppressed pair per run - the
    # honest cost of making the connection page's "a deleted post is recreated" promise true).
    # A gone/blank target -> NOT suppressed -> the pair falls through to write_target, where
    # the reconciliation table recreates (rule 3) or refills (rule 2).
    if not connector.strategy.exists_target(connector, aid, loc):
        return False
    cid = getattr(connector, "connection_id", None)
    row = idmap.get(cid, aid, loc) if cid is not None else None
    if row is None or not row.target_id:
        return False
    return connector.target_health(row.target_id) == "ok"


def _wp_build_payload(prep, loc, seg0_target, mapping):
    from app.filters import get_filter
    from app.filters.html_filter import escape_fragment_translation
    from app.services.connectors.template_integrity import SHORTCODE_SPANS, template_spans_match

    ex = prep.extra
    # Title: a NEEDS_REVIEW-excluded seg0 yields None -> fall back to the SOURCE title (a
    # real title), never a hedge. Excerpt (M5): pulled from its own mapping key; refused ->
    # source excerpt. Body: rebuild from the MASKED bytes (so block comments survive), then
    # UNMASK shortcodes back to verbatim (H3).
    title = seg0_target if seg0_target is not None else ex["source_title"]
    excerpt_t = mapping.get("excerpt")
    excerpt = ex.get("source_excerpt", "")
    if excerpt_t is not None:
        excerpt = escape_fragment_translation(excerpt, excerpt_t, _SHORTCODE_RE)
    html_mapping = {k: v for k, v in mapping.items() if k != "excerpt"}
    built = get_filter("html").build(ex["masked_body_bytes"], html_mapping).decode("utf-8")
    # Round-2 H2 integrity POSTCONDITION (the tag-repair pattern): every mask sentinel must
    # still be present byte-exact before we unmask. If the html filter / a hostile engine
    # stripped or normalized one, the shortcode is LOST - HOLD the pair (fail it, never write
    # a body with a vanished shortcode) instead of shipping broken content.
    shortcode_lost = _sentinel_count(built) != len(ex["shortcodes"])
    body = _unmask_shortcodes(built, ex["shortcodes"])
    # Validate every editable surface, including title, excerpt and HTML attributes.
    # The source skeleton also contains unmasked shortcodes in attributes/scripts.
    source_body = _unmask_shortcodes(ex["masked_body_bytes"].decode("utf-8"), ex["shortcodes"])
    shortcode_lost = shortcode_lost or any(
        not template_spans_match(source, target, SHORTCODE_SPANS)
        for source, target in (
            (ex["source_title"], title),
            (ex.get("source_excerpt", ""), excerpt_t if excerpt_t is not None else excerpt),
            (source_body, body),
        )
    )
    if shortcode_lost:
        log.warning(
            "wordpress: shortcode integrity failed for locale %s; "
            "missing, added, changed, or duplicated shortcode; not written",
            loc,
        )
    # Whole-translation collapse (title excluded AND every body unit excluded AND excerpt
    # excluded) -> the fields are just source content wearing the target label. Flag it so
    # create/fill fail the pair instead of writing an untranslated clone.
    no_translation = seg0_target is None and not html_mapping and excerpt_t is None
    out = {
        "title": title,
        "body": body,
        "locale": loc,
        "post_type": ex["post_type"],
        "source_title": ex["source_title"],
        "source_slug": ex.get("source_slug", ""),
        "source_title_unique": ex.get("source_title_unique", False),
        "_no_translation": no_translation,
        "_shortcode_lost": shortcode_lost,
    }
    if ex.get("has_excerpt"):
        out["excerpt"] = excerpt  # only send excerpt when the source had one
    return out


def _wp_compute_ok(ctx):
    if ctx.rejected and not ctx.usable:
        return False
    # L2: a run whose ONLY outcome is raw-unavailable skips (nothing translated because the
    # user lacks edit capability) is an ATTENTION state, not a clean success -> ERROR status.
    if (
        ctx.counters.get("raw_unavailable_skipped")
        and ctx.created == 0
        and ctx.updated == 0
        and ctx.skipped == 0
    ):
        return False
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not ctx.failures)
        or (not ctx.failures and (ctx.created + ctx.updated + ctx.skipped) == 0 and ctx.usable)
    )


def _wp_build_message(ctx):
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} localized post(s) across "
        f"{len(ctx.usable)} locale(s) from {len(ctx.articles)} post(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) post(s).")
    if ctx.counters.get("empty_skipped"):
        parts.append(f"Skipped {ctx.counters['empty_skipped']} empty post(s).")
    if ctx.counters.get("raw_unavailable_skipped"):
        parts.append(
            f"ATTENTION: skipped {ctx.counters['raw_unavailable_skipped']} post(s) "
            f"whose raw content was unavailable (the connection's user needs edit "
            f"capability) - NOT translated as rendered HTML."
        )
    needs_link = idmap.status_counts(ctx.cid).get("needs_link", 0)
    if needs_link:
        parts.append(
            f"{needs_link} post(s) need a link decision (a pre-existing translation "
            f"was found - adopt or ignore it)."
        )
    # M2: pairs whose human edits we overwrote (re-translation on source change is the design;
    # this makes the loss VISIBLE per pair rather than silent).
    if ctx.connector._superseded:
        sup = sorted(set(ctx.connector._superseded))
        parts.append(
            f"Superseded human edits on {len(sup)} target(s) "
            f"({', '.join(sup[:5])}) - they were re-translated from the changed "
            f"source (edits overwritten)."
        )
    # M4: explicit ids that were our OWN targets (a translation, not a source) - refused.
    if ctx.connector._named_own_skipped:
        own = sorted(set(ctx.connector._named_own_skipped))
        parts.append(
            f"Ignored {len(own)} named id(s) that are our own translation posts "
            f"({', '.join(own[:5])}) - a translation is not a source."
        )
    # M5: image alt text is translated, except in Gutenberg cover blocks (their JSON keeps
    # a second alt copy that would go out of sync). A true count, never a silent skip.
    if ctx.connector._img_alt_total:
        parts.append(
            f"Note: {ctx.connector._img_alt_total} image alt/title text(s) inside Gutenberg "
            f"cover blocks were NOT translated (the block keeps its own copy of the alt; "
            f"translate it in the block editor - see the runbook)."
        )
    # Round-2 M1: report masked shortcode spans so a residual false mask (e.g. `[sic]`) is
    # visible to the operator (shape-identical to a real no-attr shortcode, can't be excluded).
    if ctx.connector._shortcode_masked_total:
        parts.append(
            f"Note: {ctx.connector._shortcode_masked_total} shortcode span(s) were "
            f"preserved untranslated (masked) - check none are prose brackets."
        )
    # Round-2 H2: pairs HELD because a masked shortcode was lost in translation (never written
    # broken; they re-attempt next run).
    if ctx.connector._shortcode_lost_total:
        parts.append(
            f"HELD {ctx.connector._shortcode_lost_total} pair(s): a masked shortcode "
            f"was lost or a shortcode was added, changed, or duplicated "
            f"- not written (will re-attempt)."
        )
    # Round-2 M3: explicit ids on the FREE tier bypass the language fence - name the risk.
    if ctx.connector._explicit_on_free:
        parts.append(
            "Note: explicit post ids were used on the free tier - a post's language "
            "cannot be verified on this tier, so ensure they are source-language."
        )
    # M3: the Polylang mode FROM THIS RUN's probe (ctx.extra), not the stale cfg snapshot.
    mode = (ctx.extra or {}).get("mode") or ctx.connector._polylang_mode()
    parts.append(ctx.connector.mode_label(mode))
    try:
        unknown = ctx.connector.unknown_target_locales(ctx.usable)
    except Exception:
        unknown = set()
    if unknown:
        parts.append(
            f"Note: target locale(s) {', '.join(sorted(unknown))} are not (yet) "
            f"Polylang languages - the post(s) were created but the Polylang "
            f"translation link may not attach until you add the language under "
            f"Languages."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} locale(s) (source locale is never a "
            f"target): {', '.join(ctx.rejected)}."
        )
    if ctx.errors:
        parts.append("Errors: " + "; ".join(ctx.errors[:3]))
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("post write(s)").strip())
        log.warning(
            "wordpress_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    # L1: build_message runs once at end of run - close the reused target_health client here.
    ctx.connector._close_run_client()
    return " ".join(parts)


def _wp_build_result(ctx):
    return {
        "articles": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "empty_skipped": ctx.counters.get("empty_skipped", 0),
        "raw_unavailable_skipped": ctx.counters.get("raw_unavailable_skipped", 0),
        "superseded": sorted(set(ctx.connector._superseded)),
        "ignored_own_ids": sorted(set(ctx.connector._named_own_skipped)),
        "img_alt_untranslated": ctx.connector._img_alt_total,
        "shortcode_masked": ctx.connector._shortcode_masked_total,
        "shortcode_lost_held": ctx.connector._shortcode_lost_total,
        "explicit_ids_on_free": ctx.connector._explicit_on_free,
        "rejected": ctx.rejected,
        "failures": ctx.failures,
        "polylang_mode": (ctx.extra or {}).get("mode") or ctx.connector._polylang_mode(),
        "id_map": idmap.status_counts(ctx.cid),
    }


def wordpress_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate WordPress posts/pages into one or more locales, each written as a
    per-locale POST in the target language via the T3 DocPerLocaleStrategy (reserve-then-
    fill + id-map reconciliation, so the source post and other locales are never
    overwritten and a partial build resumes rather than duplicating). Raw block content is
    read and written (context=edit), so Gutenberg blocks and shortcodes survive the round
    trip. With Polylang Pro the translation is linked to the source's translation group
    (assisted corroboration); without Pro the connector runs on its own id map alone.

    Thin wrapper over flow.run_translate_flow - the 3rd T3 flow, ZERO engine edits.
    WordPress-specific behavior via hooks: raw-content extraction (get_filter('html')),
    explicit draft/publish status on both posts and pages, source-locale rejection,
    content-hash suppression, and rule-6 needs_link surfacing (Pro only). Poll-first, so
    NO watermark. only_pairs restricts the write loop to those (post_ref, locale) pairs."""
    spec = flow.TranslateFlowSpec(
        kind="wordpress",
        no_conn_msg="No such WordPress connection.",
        hash_key="wordpress_hashes",
        probe_label="WordPress",
        preflight=_wp_preflight,
        prepare=_wp_prepare,
        is_suppressed=_wp_is_suppressed,
        build_payload=_wp_build_payload,
        compute_draft=lambda cfg: (
            str(cfg.get("publish", "0")).strip().lower() not in ("1", "true", "yes")
        ),
        compute_ok=_wp_compute_ok,
        build_message=_wp_build_message,
        build_result=_wp_build_result,
        extra_counters=("empty_skipped", "raw_unavailable_skipped"),
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


def wordpress_resolve_link(
    cid: int, source_id: str, target_locale: str, action: str, actor: str = ""
) -> tuple[bool, str, dict]:
    """Operator resolution of a WordPress needs_link row (SPEC-t3 rule 6 adopt-or-ignore):
    a pre-existing Polylang-linked target post the id map never created. `action` is
    'adopt' (accept it as ours -> the next run UPDATES it) or 'ignore' (drop the row -> it
    re-flags). The one seam web + CLI + /api all call. Never overwrites content - it only
    edits the id map."""
    from app.services.connectors.store import _log, get

    conn = get(cid)
    if not conn or conn.kind != "wordpress":
        return False, "No such WordPress connection.", {}
    ok, msg = idmap.resolve_needs_link(cid, str(source_id), target_locale, action)
    _log(cid, "resolve-link", ok, 0, msg, actor)
    return ok, msg, {"id_map": idmap.status_counts(cid)}
