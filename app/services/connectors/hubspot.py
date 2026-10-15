"""HubSpot CMS connector — hand-build #4 of the connector factory.
Reference digest: bb-io/HubSpot (MIT).

TOPOLOGY — **T3 with NATIVE linkage**, re-derived (not trusted) from the digest §7.
A HubSpot translation IS a separately-addressable object (its own id, its own
`currentState`, its own slug), which rules out T1 (no per-field `{locale: value}` map)
and T2 (no `/{id}/translations/{locale}` sub-resource URL). That is T3's defining
shape. But unlike the framework's canonical T3 systems (Notion / Help Scout / GitBook,
where WE own the entire source->target mapping table), **HubSpot itself owns the
correlation**: every page/post carries `language`, `translatedFromId`, and a
`translations` dict keyed locale -> `{id, slug}`, returned on every GET with no extra
call. So this connector's `HubSpotVariantStrategy` is T3 whose `correlation_key` is
HubSpot's own dict — **we persist NO id map** (digest §7's "single biggest cost-saver")
— and whose `exists_target` is OUR OWN registered-set probe, never that dict (see
GOTCHA 3).

V1 SCOPE — **site pages, landing pages and blog posts ONLY.**
**MARKETING EMAILS AND MARKETING FORMS ARE DEFERRED, NOT HALF-BUILT.** They are the
other half of HubSpot's split shape (digest §3b): PURE T3 with *zero* native linkage —
no group, no `translatedFromId`, no `translations` dict — so they need the full
persisted-correlation `DocPerLocaleStrategy` treatment. They are deferred from v1 for
three reasons, each independently sufficient:
  1. Both live on a DIFFERENT API family (`/marketing/v3/emails`, `/marketing/v3/forms`)
     that HubSpot itself labels BETA, with a pagination envelope the digest (§5, §6.9)
     flags as unverified against the CMS `GetAllResponse<T>` shape. Building against a
     guessed envelope is how a connector ships broken.
  2. The reference's own default for them (`CreateNew=false`) OVERWRITES the source
     object in place — the source-language email is destroyed (digest §3b, §6.6). Which
     default is correct for us is a live-verifiable product decision, not a hermetic one.
  3. The reference implementation builds NO correlation table for them, so there is no
     pattern to port — only one to invent, and inventing it against unverified beta
     endpoints is exactly the half-build this build was told not to ship.
Configuring `content_types` with an email/form value is REFUSED with that explanation
(never silently ignored). See the runbook + `THIRD_PARTY_NOTICES.md`.

THE THREE DIGEST GOTCHAS ARE DESIGN REQUIREMENTS (digest §6):

  1. CONTENT FIDELITY — **we do NOT port the `<body original="...">` encoding at all.**
     The reference HTML-encodes the ENTIRE `layoutSections` JSON into ONE `<body>`
     attribute and depends on that attribute surviving every pipeline stage byte-intact
     (digest §6.1: "the sharpest structural risk in the whole connector"). Our contract
     does not need it: the flow engine takes translation UNITS (text + p_index) and hands
     back per-unit translations, so we walk the module-tree JSON directly, emit one unit
     per translatable leaf, and reinsert each translated string at its exact path into a
     DEEP COPY of the CURRENT target JSON. Nothing but the translated leaves is ever
     re-serialized through a lossy channel, so structural fidelity is guaranteed by
     construction rather than by an attribute surviving N tools. Proven by test
     (`verify_hubspot`, section 4): a page whose layoutSections carry ids, css classes,
     numbers, booleans, nulls, unicode, HTML entities and deep nesting round-trips
     byte-identical except the translated leaves.

  2. DRAFT-VS-PUBLISHED (the Strapi-H4 shape) — **DECIDED: v1 reads, probes and writes
     the DRAFT sub-resource, and NEVER pushes live.** The Pages/Blog APIs expose BOTH a
     base object endpoint and a documented `/draft` sub-resource with `push-live`/`reset`
     actions; the reference uses the base for all three and never touches `/draft`
     (digest §6.3, UNCONFIRMED). We take the draft, because:
       * it matches the framework's write-as-draft-promote-on-completion law (design
         §3.5) and HubSpot's own model (a fresh language variation starts in DRAFT and
         publishing is always a separate action, digest §3a);
       * it means a run can NEVER overwrite live content — including a pre-existing
         HUMAN translation, which stays live until a person reviews the draft and pushes
         it. That is this connector's headline safety property.
     READ, PROBE and WRITE all target the SAME state, always — they are derived from one
     `content_state` setting, so they cannot drift apart. The setting is an operator
     escape hatch (`draft` default | `published`) that flips ALL THREE together; it is
     validated fail-closed and NEVER pushes live in either mode. The second-run steady
     state is proven for BOTH modes by test (sections 6 + 6b), and the fake stores the
     draft and published representations SEPARATELY so a read/probe/write mismatch FAILS
     the suite rather than passing silently (the Strapi fixture lesson, taxonomy G).
     RUNBOOK MANDATORY-1 verifies on a real portal that `GET/PATCH .../{id}/draft`
     exists, returns the `translations` dict, and works for a never-published object;
     the documented revert if it does not is `content_state=published` — config only,
     no code change.

  3. THE PHANTOM-CLONE WINDOW — `create-language-variation` CLONES the full source
     content into a new object in DRAFT *before* our translated PATCH lands (digest §6.4),
     so a crash in that window leaves a real, gettable variant tagged with the TARGET
     locale carrying 100% SOURCE text. Two mechanisms, both required:
       * **The existence probe never reads the `translations` dict.** It reads OUR OWN
         registered set — `hubspot_variants` (the variant id we wrote) + `hubspot_written`
         (the exact property/JSON paths we successfully wrote) persisted per successful
         pair via `flow.on_write_ok`, BEFORE the end-of-run hash merge (the Shopify H1
         ordering, so a crash can never leave a hash without its tracking). A phantom has
         NO tracking, so it is never "translated". Untracked => NOT covered, with no
         any-key fallback. The probe additionally requires every tracked path to still
         carry a non-empty value in the live object, so a human deleting a translated
         module surfaces it for restore (the Shopify MEDIUM-3 full-coverage shape).
       * **Create-then-update is crash-tolerant and never duplicates.** The write path
         ALWAYS resolves the variant from HubSpot's own `translations` dict FIRST and
         reuses it; `create-language-variation` is called only when that dict has no
         entry for the locale. So a re-run after a crash COMPLETES the phantom (PATCHes
         the translation into it) instead of skipping it or cloning a second one — and it
         needs no persisted state to do so, because the native linkage IS the ledger.
       * Corollary (digest §6.5): we ALWAYS resolve to the language group's true PRIMARY
         (`translatedFromId`) before calling create-language-variation — unconditionally,
         never the legacy `BasePageActions` shortcut — so a variation-of-a-variation is
         impossible. The fake REFUSES a create-language-variation aimed at a non-primary
         id, which is what makes that assertion non-vacuous.

FIELD SELECTION (taxonomy D / digest §6.2). HubSpot has no per-module schema API, so
selection gates on BOTH the property NAME and the VALUE (review HIGH-1 — a name gate
alone let a HubL `module_block`, a `{{ page.name }}` expression, a CDN URL and a contact
email reach MT and the wire, because module authors put all four under ordinary names
like `text`/`title`/`content`/`label`):
  * NAME — `_NEVER_TRANSLATE` identity names always lose; then the operator `fields`
    allowlist, else `_TEXT_PROPERTIES` minus `exclude_fields`. `value` and `name` are
    excluded DELIBERATELY (HubSpot choice modules carry the machine value in `value` and
    the display copy in `label`); both appear in the skipped report so an operator can
    allowlist them per portal.
  * VALUE — `_value_is_machine` rejects URLs, emails, ISO date/times, letter-free values
    and ENTIRELY-expression HubL, in BOTH modes (see its comment for why the allowlist
    does not bypass it). Value SHAPE alone never rejects: "Fast", "Subscribe" and "Learn
    more" are copy and must pass.
The value gate applies to EVERY outgoing string, not just module leaves: `name`,
`htmlTitle`, `metaDescription` and each `postBody` paragraph all funnel through
`_prepare_value` (round-2 HIGH-1 — the gate previously had a single call site, so a HubL
SEO-title template, a documented HubSpot feature and therefore the likelier carrier,
sailed through while the identical string in a module property was blocked).

HUBL TOKEN PROTECTION (round-2 MEDIUM-4). Rejecting any value containing HubL made real
copy untranslatable — "Welcome back, {{ contact.firstname }}! Save 20% today." is a
sentence. So a MIXED HubL+prose value is MASKED, not refused: `mask_hubl` swaps each
`{{ ... }}` / `{% ... %}` span for a shared-primitive placeholder token (`segment.ph_point`,
`⟦hN⟧` — already in `segment`'s protected-span list, so segmentation, the QE gate and the
tag machinery all understand it), MT translates around it, and `unmask_hubl` restores the
span byte-exact. A token that did NOT survive intact makes the field behave exactly like a
held-back one: OMITTED and reported, never written broken. Only an ENTIRELY-expression
value (no letters left once the HubL spans are removed) is still refused outright.
The reference's silent-skip defect is closed on the reporting side too: EVERY unselected
non-empty string leaf that CONTAINS A LETTER is reported in the run message + result
(deduped by property name, actionable reasons ranked first, cap 8 + "+N more"), and an
allowlist miss is logged once. The earlier prose heuristic (>=12 chars AND a space) was
itself a silent-skip bug — it excluded the commonest custom-module strings
(`cta_label="Subscribe"`, `hero_sub="Fast"`), reporting `fields_skipped: {}`.

TITLE / META. `name` is Segment order 0. `htmlTitle` and `metaDescription` ride as
named meta units. A HELD-BACK (NEEDS_REVIEW) title is OMITTED from the PATCH — it is
NEVER source-filled (that is Strapi H2 exactly), so a stored translated title survives.

BODY MERGE — the framework's cardinal T1/T3 rule (design §2), read-CURRENT-merge-write:
  * pages/landing pages: deep-copy the VARIANT's CURRENT `layoutSections` and set only
    the translated paths. An omitted (held-back) path keeps the variant's existing
    translated value; a path that no longer exists in the variant is skipped + REPORTED.
  * blog posts: `postBody` is one HTML string through the shared `html` filter. The
    skeleton is the VARIANT's current postBody when its unit count matches the source's
    (so a held-back paragraph keeps its stored translation); on real structure drift with
    ANY unit held back we OMIT postBody entirely and report incomplete, rather than let
    `html.build`'s source-fallback skeleton write source over a translation.

RATE LIMITS / RETRY (digest §6.8). We do NOT hand-roll a retry loop: every call goes
through the shared `base.Connector._request`, which already retries 429 + 5xx with the
numeric `Retry-After` honored and capped (repo law: a connector that hand-rolls a shared
primitive is wrong even if it works). HubSpot's HTML-error-page edge case (digest §6.13)
IS handled — a non-JSON body is turned into a clear ConnectorError, never a crash.

DEFERRED FROM v1 (studied, deliberately not half-built — see the runbook + report):
marketing emails + marketing forms (above), HubDB (no localization model at all, digest
§7), OAuth 3LO (v1 = Service Key or Private App token, both Bearer; the digest notes
the scope names are identical and nothing branches on OAuth internally, so it is
additive later), internal-link
relocalization (`content_id` rewriting — the reference silently no-ops it on a missing
target, digest §6.7), slug overwrite from the translated file (off by default in the
reference too; a careless enable produces URL collisions), business-unit filtering,
per-module HTML segmentation of page module values (a page module's `content`/`html`
property is translated whole, like Strapi/Shopify v1 — only blog `postBody` is segmented),
webhooks (HubSpot has none for this surface — the reference's "Events" folder is 100%
polling, digest §4), and push-live/schedule-publish (a deliberate human action).

PROVENANCE: endpoint shapes are reference-and-reimplemented from bb-io/Hubspot
((c) 2023 Blackbird International Inc., MIT — verified verbatim at build time). The
sibling repo bb-io/HubspotCRM has NO license file and was NOT read or referenced. See
THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import copy
import json
import logging
import re

from app.services.connectors import base, flow, harvest, webhooks  # noqa: F401
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
    fetch_cap,
    harvest_context_field,
    item_chars,
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
    _write_lock_for,
)
from app.services.connectors.template_integrity import HUBL_SPANS, template_spans_match
from app.services.connectors.vendor_text import vendor_quote

log = logging.getLogger("loc_tms.connectors")

# HubSpot's CMS content API. Pinned explicitly (design §3.6: an unpinned version means
# payloads shift under you). Not user input — a module constant — so no injection concern.
API_VERSION = "v3"
API_BASE = f"https://api.hubapi.com/cms/{API_VERSION}"

# The content types this v1 supports, and their URL segment + body model.
#   body="layout" -> `layoutSections` module tree (our JSON path walker)
#   body="html"   -> `postBody` HTML string (the shared `html` filter)
_CONTENT_TYPES = {
    "site-pages": {"segment": "pages/site-pages", "body": "layout", "label": "Site page"},
    "landing-pages": {"segment": "pages/landing-pages", "body": "layout", "label": "Landing page"},
    "blog-posts": {"segment": "blogs/posts", "body": "html", "label": "Blog post"},
}
_SUPPORTED_TYPES = tuple(_CONTENT_TYPES)

# Content types that EXIST in HubSpot (and in the reference) but are deliberately NOT
# shipped in v1 — named explicitly so configuring one gets the real reason rather than a
# generic "unsupported" (see the module docstring's V1 SCOPE block).
_DEFERRED_TYPES = {
    "marketing-emails": "marketing emails",
    "emails": "marketing emails",
    "marketing-forms": "marketing forms",
    "forms": "marketing forms",
    "hubdb": "HubDB tables",
}

# Read/probe/write state (GOTCHA 2). ALL THREE derive from this one setting, so they
# cannot drift apart. Neither mode ever pushes a draft live.
_STATE_DRAFT = "draft"
_STATE_PUBLISHED = "published"
_STATES = (_STATE_DRAFT, _STATE_PUBLISHED)

# A HubSpot content object id is a numeric string. Bounded so a pathological id can
# never reach the wire (taxonomy A; the Shopify LOW-2 precedent).
_OBJECT_ID_RE = re.compile(r"^[0-9]{1,20}$")

# A HubSpot language tag as it appears in `translations` / `language`: BCP-47-ish,
# letters + digits + hyphens only. Validated before it is ever interpolated into a URL
# or a request body (taxonomy A).
_LANG_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*$")

# Translatable module-property NAMES (digest §6.2 — HubSpot exposes no per-module schema,
# so an allowlist is the only option; the reference's is 24 hardcoded names). Kept
# EXPLICIT and tight (the Strapi N2 lesson: a looser set eats real copy AND selects junk).
# `value` and `name` are excluded ON PURPOSE: HubSpot choice/select modules carry the
# MACHINE value in `value` and the display copy in `label`, and `name` is a module's
# internal name. Both are reported as skipped so an operator can allowlist them per
# portal via `fields`.
_TEXT_PROPERTIES = frozenset(
    {
        "content",
        "html",
        "title",
        "subtitle",
        "heading",
        "subheading",
        "header",
        "label",
        "text",
        "body",
        "description",
        "caption",
        "alt",
        "summary",
        "button_text",
        "link_text",
        "cta_text",
        "quote_text",
        "speaker_name",
        "speaker_title",
        "message",
        "placeholder",
        "top_label",
        "help_text",
        "rich_text",
        "headline",
        "footer_text",
    }
)

# Property names that are NEVER translatable regardless of the allowlist — uniqueness- or
# machine-bearing (translating one corrupts routing/identity). Applied even when an
# operator `fields` allowlist is set, because a URL slug or an element id has no
# translatable reading. Checked as EXACT names + a suffix rule.
_NEVER_TRANSLATE = frozenset(
    {
        "id",
        "slug",
        "url",
        "href",
        "src",
        "path",
        "css",
        "css_class",
        "class",
        "type",
        "tag",
        "key",
        "locale",
        "language",
        "email",
        "domain",
        "target",
        "widget_name",
        "module_id",
        "content_id",
        "form_id",
        "hubdb_table_id",
    }
)
_NEVER_SUFFIXES = ("_id", "_url", "_src", "_href", "_class", "_css", "_path", "_slug")

# --- VALUE-shape gate (review HIGH-1) ---------------------------------------------
# A name allowlist ALONE is not enough: HubSpot module authors put HubL expressions,
# CDN URLs and contact emails under perfectly ordinary property names (`text`, `title`,
# `content`, `label`), and the reviewer's probe proved all four reached MT and were
# WRITTEN. So selection gates on the VALUE too — the already-reviewed Strapi/Contentful
# `_value_is_machine` port, plus a HubL clause this connector needs and they do not.
#
# Unlike Contentful (where the operator `fields` allowlist bypasses every heuristic), the
# VALUE gate here applies in BOTH modes. The divergence is deliberate: an allowlist entry
# says "this PROPERTY holds copy", never "translate this particular machine value", and a
# mangled HubL expression is not a bad translation — it is a page that no longer renders,
# which is not recoverable by re-translating. The NAME allowlist still overrides the name
# heuristic, which is what an operator actually wants it for.
_HUBL_SPAN_RE = HUBL_SPANS
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^(?:https?|ftp)://|^//[^/]|^www\.", re.I)
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_TIME_RE = re.compile(r"^\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?$")
_HAS_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def mask_hubl(text: str) -> tuple[str, list[str]]:
    """Replace every HubL expression, statement, or comment with an inert PLACEHOLDER
    TOKEN, returning (masked_text, [original spans in order]).

    Review round-2 MEDIUM-4. Rejecting any value containing HubL made a large class of
    legitimate copy untranslatable — "Welcome back, {{ contact.firstname }}! Save 20%
    today." is a sentence, not an expression. The repo already has the right pattern for
    this (inline-tag fidelity): mask the opaque span, translate around it, restore it
    byte-exact. We reuse the SHARED placeholder primitive (`segment.ph_point`, tokens
    `⟦hN⟧`) rather than inventing a marker — those tokens are already in `segment`'s
    protected-span list, so segmentation, the QE gate and the tag machinery all understand
    them, and a connector that hand-rolls a shared primitive is wrong even if it works."""
    from app.services import segment as _seg

    spans: list[str] = []

    def _sub(m):
        spans.append(m.group(0))
        return _seg.ph_point("h", len(spans))

    return _HUBL_SPAN_RE.sub(_sub, text or ""), spans


def unmask_hubl(text: str, spans: list, source: str | None = None) -> tuple[str, bool]:
    """Restore the original HubL spans into a translated string.

    Returns (restored, ok). Added or changed spans also fail, even without source masks.
    `source` supplies the original field for checking entity-encoded literal spans.
    `ok` is False when ANY token did not survive translation
    byte-exact — dropped, duplicated, or mangled by the engine. A caller MUST treat
    ok=False as held: writing a half-restored template is a page that no longer renders,
    which is strictly worse than not writing at all (the tag-integrity discipline: source
    tag-count != target tag-count => needs_review, keep the original)."""
    from app.services import segment as _seg
    from app.services.connectors.template_integrity import template_spans_match

    out = text or ""
    for i, span in enumerate(spans or (), start=1):
        tok = _seg.ph_point("h", i)
        if out.count(tok) != 1:
            return text or "", False
        out = out.replace(tok, span)
    # A leftover/invented token means the engine emitted marker-ish garbage; refuse it.
    if _seg._PLACEHOLDER_RE.search(out):
        return text or "", False
    original = source if source is not None else "".join(spans or ())
    return out, template_spans_match(original, out, _HUBL_SPAN_RE)


def _hubl_free(value: str) -> str:
    """`value` with every HubL span removed — what a human would actually read."""
    return _HUBL_SPAN_RE.sub(" ", value or "")


def _value_is_machine(value: str) -> str | None:
    """A skip reason when the VALUE is an unambiguous machine format, else None.

    Value SHAPE alone (a short or hyphenated string) is NOT a machine format — "Fast",
    "Subscribe" and "Learn more" are real copy and must pass this gate (they are exactly
    the class the previous prose heuristic wrongly discarded).

    HubL (round-2 MEDIUM-4): only an ENTIRELY-expression value is rejected here — one
    whose human-readable remainder, after the HubL spans are removed, has no letters
    (`{% module_block ... %}`, `{{ page.name }} | {{ content.name }}`). MIXED HubL + prose
    passes and is protected by `mask_hubl` instead, so no legitimate copy is unreachable.
    The round-1 "mangled HubL = unrenderable page" justification still stands, but it only
    ever justified the entirely-expression case."""
    v = (value or "").strip()
    if _HUBL_SPAN_RE.search(v) and not _HAS_LETTER_RE.search(_hubl_free(v)):
        return (
            "HubSpot template expression ({{ }} / {% %}) with no translatable text - "
            "translating it breaks rendering"
        )
    if _EMAIL_RE.match(v):
        return "email-shaped value"
    if _URL_RE.match(v):
        return "URL-shaped value"
    if _DATETIME_RE.match(v) or _TIME_RE.match(v):
        return "date/time-shaped value"
    if not _HAS_LETTER_RE.search(v):
        return "no letters in the value (code/number/punctuation)"
    return None


# WriteResult.message marker for a pair whose EVERY translatable field was held back
# (NEEDS_REVIEW) this run: ok=False so the flow records NO suppression hash (taxonomy F),
# then accounted as `held_back` rather than a real failure.
_HELD_BACK_PREFIX = "held-back:"
_HELD_BACK = (
    _HELD_BACK_PREFIX + " every translatable field is awaiting review "
    "(NEEDS_REVIEW); nothing written, existing translation (if any) kept"
)

# Tracking config maps (GOTCHA 3). Persisted per successful pair by
# _hubspot_on_write_ok, BEFORE the end-of-run hash merge (Shopify H1 ordering).
# Keys carry the CONTENT STATE (review HIGH-2) — see HubSpotConnector.tracking_key.
_VARIANTS_KEY = "hubspot_variants"  # {"<ctype>:<id>:<loc>:<state>": "<variant id>"}
_WRITTEN_KEY = "hubspot_written"  # {"<ctype>:<id>:<loc>:<state>": ["<path token>", ...]}


class _Unit:
    """A minimal flow translation unit (the engine reads only .text + .p_index).
    p_index is our reinsertion address: "m:<prop>" for a meta property, "b<i>" for the
    i-th layoutSections leaf of this prep, or the html filter's own int p_index for a
    blog post's postBody."""

    __slots__ = ("p_index", "text")

    def __init__(self, text: str, p_index):
        self.text = text
        self.p_index = p_index


def _parse_names(raw) -> list[str]:
    out: list[str] = []
    for x in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        n = x.strip()
        if n and n not in out:
            out.append(n)
    return out


def _path_token(path) -> str:
    """A tracked path's stable string token: a JSON-encoded key/index list. JSON so a
    module key containing '/', ':' or a digit can never be confused with a list index —
    the tracking probe walks the decoded list, never a re-split string."""
    return "body:" + json.dumps(list(path), separators=(",", ":"))


def _prop_token(prop: str) -> str:
    return f"prop:{prop}"


def _walk_path(container, path):
    """Follow a decoded key/index path into a nested dict/list, or ABSENT."""
    cur = container
    for step in path:
        if isinstance(step, int):
            if not isinstance(cur, list) or not (0 <= step < len(cur)):
                return ABSENT
            cur = cur[step]
        else:
            if not isinstance(cur, dict) or step not in cur:
                return ABSENT
            cur = cur[step]
    return cur


def _set_path(container, path, value, protected=None) -> bool:
    """Set `value` at `path` inside `container` IN PLACE. Returns False (writing nothing)
    when the path does not resolve to an existing string leaf — a structural change in the
    target since extraction. We never CREATE structure in the target: inventing a module
    key HubSpot's renderer does not expect is how a layout breaks."""
    from app.filters.html_filter import escape_fragment_translation

    if not path:
        return False
    cur = container
    for step in path[:-1]:
        if isinstance(step, int):
            if not isinstance(cur, list) or not (0 <= step < len(cur)):
                return False
            cur = cur[step]
        else:
            if not isinstance(cur, dict) or step not in cur:
                return False
            cur = cur[step]
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(cur, list) or not (0 <= last < len(cur)):
            return False
        if not isinstance(cur[last], str):
            return False
        cur[last] = escape_fragment_translation(cur[last], value, _HUBL_SPAN_RE, protected)
        return True
    if not isinstance(cur, dict) or last not in cur or not isinstance(cur[last], str):
        return False
    cur[last] = escape_fragment_translation(cur[last], value, _HUBL_SPAN_RE, protected)
    return True


SPEC = ConnectorSpec(
    kind="hubspot",
    name="HubSpot CMS",
    icon="\U0001f9e1",  # orange heart (HubSpot's brand colour)
    cat="CMS",
    blurb="Translate HubSpot site pages, landing pages and blog posts into any language. "
    "Each translation is a real HubSpot language variation, linked into the page's "
    "own multi-language group (HubSpot tracks the source/target correlation, we "
    "never invent one). Translations are written to the object's DRAFT by default "
    "and never published — review and push live in HubSpot. Marketing emails and "
    "marketing forms are NOT supported in this version (they have no native "
    "language link and their APIs are HubSpot-beta).",
    config_fields=[
        FieldSpec(
            "content_types",
            "Content types",
            "Comma-separated HubSpot content "
            "types to translate: any of site-pages, landing-pages, blog-posts "
            "(default site-pages). Marketing emails and forms are not supported "
            "in this version.",
            required=False,
            placeholder="site-pages, blog-posts",
        ),
        FieldSpec(
            "content_state",
            "Content state",
            "Which version of each object to "
            "read, probe and write: 'draft' (default, recommended - never touches "
            "live content; review and push live in HubSpot) or 'published' (edits "
            "the live object in place). All three operations always use the same "
            "state.",
            required=False,
            placeholder="draft",
        ),
        FieldSpec(
            "source_language",
            "Source language",
            "The language your pages are "
            "authored in, as HubSpot tags it (default 'en'). Used as the language "
            "group's primary and dropped from the target set.",
            required=False,
            placeholder="en",
        ),
        FieldSpec(
            "fields",
            "Translatable properties",
            "Optional allowlist of module "
            "property names to translate (comma-separated, e.g. 'content, html, "
            "headline'). Blank = HubSpot's common text properties; the run report "
            "lists what was selected AND what was skipped so custom modules with "
            "unusual property names are never silently missed.",
            required=False,
            placeholder="content, html, headline",
        ),
        FieldSpec(
            "exclude_fields",
            "Exclude properties",
            "Module property names to "
            "NEVER translate (comma-separated). Applies only when the allowlist "
            "above is blank.",
            required=False,
            placeholder="alt, caption",
        ),
        FieldSpec(
            "translate_meta",
            "Translate SEO title/description",
            "Also translate "
            "each object's htmlTitle and metaDescription. On by default; set 0 to "
            "skip them.",
            required=False,
            placeholder="1",
        ),
        FieldSpec(
            "target_locales",
            "Target languages",
            "Comma-separated HubSpot "
            "language tags to create variations for (e.g. 'fr, de, es-mx').",
            required=False,
            placeholder="fr, de",
        ),
        FieldSpec(
            "max_entries",
            "Max objects",
            "Safety cap on objects scanned per run "
            "(shared across content types, filled round-robin so no one type "
            "starves the others).",
            required=False,
            placeholder="50",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "access_token",
            "Service Key",
            "A HubSpot Service Key "
            "(Settings -> Integrations -> Service Keys) with the "
            "'content' scope. Sent as an Authorization: Bearer header. Older "
            "accounts that still have a Private App access token can use it the same way.",
            secret=True,
            placeholder="pat-na1-...",
        ),
    ],
    setup_steps=[
        "In HubSpot, go to Settings -> Integrations -> Service Keys and create a key. "
        "Service Keys replace Private Apps for new accounts. You need Developer Tools access. "
        "A separate developer account is not needed (developer accounts were merged "
        "into standard accounts in 2026).",
        "Grant the key the 'content' scope (CMS pages, landing pages and blog posts, "
        "read + write). No other scope is needed for this version.",
        "Create the key and copy it. It is sent as the same Bearer token a Private App "
        "token used (an older 'pat-' token still works).",
        "Add each target language to your site's multi-language setup in HubSpot "
        "(Settings -> Website -> Pages -> your domain). Note that HubSpot's FREE tools "
        "cap you at 3 language variations per page.",
        "Enter the content types (e.g. 'site-pages'), the access token and your target "
        "languages. Leave 'Content state' on 'draft' unless you have a reason not to.",
    ],
    other_side="Change detection is poll-based: run on a schedule. HubSpot publishes no "
    "webhooks for CMS content, so there is nothing to register on their side. "
    "Each translation is a real HubSpot language variation linked into the "
    "page's own multi-language group; the source page is never overwritten. "
    "Translations land on each variation's DRAFT and are NOT published - "
    "review them in HubSpot and push live when you are happy. Marketing "
    "emails and marketing forms are not supported in this version.",
    docs_url="https://developers.hubspot.com/docs/api-reference/cms-pages-v3",
    supports_oauth=False,
)


class HubSpotVariantStrategy(LocaleMappingStrategy):
    """T3 write-back over HubSpot's NATIVE language-variation linkage.

    Vendor-neutral in the same spirit as the shared strategies: it drives a small client
    protocol the connector implements (kept on the connector because these need its authed
    HTTP client and its content-type routing):

        connection_id                                   -> keys the shared D1 write lock
        resolve_primary(aid) -> aid | ABSENT            (walk translatedFromId to the group primary)
        native_variant(aid, locale) -> variant_aid|None (read HubSpot's OWN translations dict)
        create_variation(primary_aid, locale)           -> (variant_aid | None, error)
        fetch_object(aid) -> dict | ABSENT              (in the configured content state)
        patch_object(aid, body) -> (ok, error)          (in the configured content state)
        tracked(aid, locale) -> (variant_aid|None, set[path token])
        _record_written(aid, locale, variant_aid, tokens)
        _record_incomplete(aid, locale, {field: reason})

    `record_id` here is our composite `"<content-type>:<numeric id>"` (validated
    fail-closed before it ever reaches a URL — taxonomy A), and `fields` is the payload
    `_hubspot_build_payload` produced.

    WHY THERE IS NO PERSISTED ID MAP (digest §7): HubSpot's `translations` dict IS the
    correlation, returned on every GET. Persisting our own would only duplicate — and
    could disagree with — the system of record. The `idmap`/DocPerLocaleStrategy
    reconciliation table exists for T3 systems with NO native link (Notion, Help Scout);
    engaging it here would be hand-rolling a primitive the source system already provides.

    WHY exists_target DOES NOT READ THAT DICT (GOTCHA 3): a variant's mere EXISTENCE
    proves nothing — `create-language-variation` clones 100% source text into it before
    our PATCH lands, so dict-presence would mark a phantom "translated" forever. The
    probe therefore reads OUR OWN registered set and re-verifies it against the live
    object.
    """

    topology = "T3"

    def exists_target(self, client, record_id: str, target_locale: str) -> bool:
        """True iff we TRACKED a variant for this pair AND that variant still exists AND
        every path we recorded writing still carries a non-empty value.

        Untracked => NOT covered, deliberately with no fallback (the Shopify H1 shape):
        a phantom clone, a pair whose hash predates this build, or a crash before tracking
        landed all cost at most ONE re-write that re-establishes tracking — whereas any
        'the variant exists, call it done' fallback seals a 100%-source-text page as
        translated forever. Requiring the tracked PATHS (not just the id) means a human
        deleting a translated module surfaces the pair for restore (Shopify MEDIUM-3)."""
        variant_aid, tokens = client.tracked(record_id, target_locale)
        if not variant_aid or not tokens:
            return False
        obj = client.fetch_object(variant_aid)
        if obj is ABSENT or not isinstance(obj, dict):
            return False
        return all(self._token_present(obj, tok) for tok in tokens)

    @staticmethod
    def _token_present(obj: dict, token: str) -> bool:
        """Is the value we once wrote at `token` still present and non-empty? A PRESENCE
        check on our own registered path — never a comparison against source or against
        the translation we sent (which would re-write on every human edit)."""
        if token.startswith("prop:"):
            return bool(str(obj.get(token[5:]) or "").strip())
        if token.startswith("body:"):
            try:
                path = json.loads(token[5:])
            except (TypeError, ValueError):
                return False  # unreadable token -> not covered (fail toward a re-write)
            val = _walk_path(obj.get("layoutSections") or {}, path)
            return isinstance(val, str) and bool(val.strip())
        return False

    def write_target(
        self, client, record_id: str, target_locale: str, fields: dict, *, draft: bool = True
    ) -> WriteResult:
        # `draft` is accepted for the framework signature but is NOT the state switch:
        # which state we touch comes from the connection's `content_state`, because the
        # READ and the PROBE must use the same one (gotcha 2) and they have no access to
        # this per-call flag. v1 never pushes a draft live in either state.
        payload = fields or {}
        if not payload.get("props") and payload.get("body") is None:
            # Every translatable field held back (NEEDS_REVIEW): write NOTHING and report
            # held-back, so the flow records NO suppression hash and the pair is
            # re-attempted next run (taxonomy F).
            reasons = "; ".join(sorted(set(payload.get("held_reasons", {}).values())))
            return WriteResult(
                False, False, message=_HELD_BACK + ("; " + reasons if reasons else "")
            )
        # Serialize the resolve->create->patch span for this (connection, object, locale)
        # so two in-process writers cannot both call create-language-variation. Same
        # single-process caveat as the shared strategies (ADR-003 Tier 3).
        lock = _write_lock_for(getattr(client, "connection_id", None), record_id, target_locale)
        with lock:
            primary = client.resolve_primary(record_id)
            if primary is ABSENT:
                return WriteResult(False, False, message=f"{record_id} not found (deleted?)")
            # GOTCHA 3, half 2: ALWAYS consult HubSpot's own linkage first. A variant left
            # behind by a crashed run (a phantom, 100% source text) is FOUND here and
            # COMPLETED rather than duplicated or skipped. Only a genuinely absent locale
            # reaches create_variation.
            variant = client.native_variant(primary, target_locale)
            created = False
            if variant is None:
                # digest §6.5: the clone is taken off the group's true PRIMARY,
                # unconditionally — never off whatever id we were handed, so a
                # variation-of-a-variation cannot be created.
                variant, err = client.create_variation(primary, target_locale)
                if err or not variant:
                    return WriteResult(
                        False, True, message=err or "create-language-variation returned no id"
                    )
                created = True
            # REVIEW MEDIUM-4 — the merge base MUST be read fresh here, not taken from the
            # per-run cache. HubSpot offers no version/ETag precondition (unlike
            # Contentful's X-Contentful-Version or Shopify's digest), so there is nothing
            # to lock against and the read->PATCH span is a genuine lost-update window.
            # Reading at flow stage 6 stretched that window across the ENTIRE MT duration;
            # reading here shrinks it to GET->PATCH, which is the most this API allows.
            # The residual is named honestly in the runbook (MANDATORY-5).
            obj = client.fetch_object(variant, use_cache=False)
            if obj is ABSENT or not isinstance(obj, dict):
                # REVIEW MEDIUM-6 — self-heal once before failing. The REACHABLE cause is
                # a variation that was DELETED in HubSpot between our group read and our
                # object read; re-reading the group either drops the locale (so we create a
                # replacement) or re-points it, and only if neither holds is the STATE the
                # problem.
                #
                # ROUND-2 MEDIUM-2 — `if not created`. Running the heal after a create we
                # JUST made was a duplication bug, not a heal: a clone with no
                # representation in the configured state (e.g. a brand-new variation has no
                # `published` version) re-entered create_variation and produced a SECOND
                # language variation. A variation we created this instant is by definition
                # not stale, so there is nothing to heal — go straight to the diagnosis.
                if not created:
                    healed = client.native_variant(primary, target_locale)
                    if healed is None:
                        variant, err = client.create_variation(primary, target_locale)
                        if err or not variant:
                            return WriteResult(
                                False,
                                True,
                                message=(err or "create-language-variation returned no id"),
                            )
                        created = True
                        obj = client.fetch_object(variant, use_cache=False)
                    elif healed != variant:
                        probe = client.fetch_object(healed, use_cache=False)
                        if probe is not ABSENT and isinstance(probe, dict):
                            variant, obj = healed, probe
                if obj is ABSENT or not isinstance(obj, dict):
                    # ROUND-2 MEDIUM-3 — the remedy must match the mode. Advising
                    # "set content_state to 'published'" while ALREADY in published mode
                    # was backwards; in that mode the correct diagnosis is a draft-only
                    # variation.
                    if client.content_state == _STATE_DRAFT:
                        remedy = (
                            "the variation may have no pending draft (a "
                            "published-only variation has nothing at /draft). "
                            "Create a draft for it in HubSpot, or set this "
                            "connection's content_state to 'published' to write the "
                            "live object."
                        )
                    else:
                        remedy = (
                            "the variation may exist only as an unpublished draft "
                            "(a draft-only variation has no published version). "
                            "Publish it in HubSpot, or set this connection's "
                            "content_state to 'draft' to write the draft instead."
                        )
                    return WriteResult(
                        False,
                        created,
                        target_id=variant,
                        message=(
                            f"language variation {variant} for {target_locale} exists in the "
                            f"group but its '{client.content_state}' representation does not "
                            f"exist - {remedy}"
                        ),
                    )
            body, tokens, held, replaced = self._build_patch(client, obj, payload)
            if not body:
                if held:
                    client._record_incomplete(record_id, target_locale, held)
                # Nothing landed: every field we had drifted out of the target's
                # structure. Retryable, NO hash (the pair returns next run).
                return WriteResult(
                    False,
                    created,
                    target_id=variant,
                    message=(
                        "no writable fields (the target's structure no longer matches the "
                        "source): " + "; ".join(f"{k} ({v})" for k, v in sorted(held.items()))
                    ),
                )
            ok, err = client.patch_object(variant, body)
            if not ok:
                return WriteResult(False, created, target_id=variant, message=err or "PATCH failed")
            client._record_written(record_id, target_locale, variant, tokens)
            if held:
                client._record_incomplete(record_id, target_locale, held)
            if replaced:
                client._record_replaced(record_id, target_locale, replaced)
            return WriteResult(
                True, created, target_id=variant, message="created" if created else "updated"
            )

    @staticmethod
    def _build_patch(client, obj: dict, payload: dict):
        """(patch_body, tracked_tokens, held) for one variant.

        The framework's cardinal T1/T3 rule (design §2): read the CURRENT target and MERGE
        into it — never blind-PUT a partial and never rebuild from source. Only the
        properties/paths this run actually translated are set; everything else on the
        object (including a stored translation of a HELD-BACK field) is left exactly as it
        is, which is the taxonomy-B guarantee expressed structurally rather than by a
        guard that has to remember every field."""
        body: dict = {}
        tokens: list[str] = []
        held: dict = {}
        replaced: dict = {}  # fields that WERE written but superseded target edits
        for prop, value in (payload.get("props") or {}).items():
            body[prop] = value
            tokens.append(_prop_token(prop))
        bodyspec = payload.get("body")
        if bodyspec and bodyspec.get("kind") == "layout":
            # GOTCHA 1: deep-copy the CURRENT layoutSections and set ONLY the translated
            # leaves. Everything untranslated is carried through by structural identity —
            # no encoding, no attribute, nothing to corrupt.
            current = obj.get("layoutSections")
            if not isinstance(current, dict):
                held["layoutSections"] = "the target has no layoutSections to merge into"
            else:
                merged = copy.deepcopy(current)
                wrote = 0
                for path, value in bodyspec.get("sets") or ():
                    protected = (bodyspec.get("protected") or {}).get(_path_token(path))
                    if _set_path(merged, path, value, protected):
                        tokens.append(_path_token(path))
                        wrote += 1
                    else:
                        held[_path_token(path)] = (
                            "this module property no longer exists "
                            "in the target - skipped, not created"
                        )
                if wrote:
                    body["layoutSections"] = merged
        elif bodyspec and bodyspec.get("kind") == "html":
            html_filter = bodyspec["filter"]
            mapping = bodyspec.get("mapping") or {}
            complete = bool(bodyspec.get("complete"))
            current = obj.get("postBody")
            source_bytes = bodyspec["source_bytes"]
            skeleton = None
            if isinstance(current, str) and current:
                cur_bytes = current.encode("utf-8")
                try:
                    # BODY units only: the html filter also emits accessibility units
                    # (image alt/title, link title; `a11y:*` style) AFTER the body, and a
                    # target that gained a paragraph and lost an alt must not pass as
                    # same-shape (the body would then land one paragraph off).
                    same_shape = (
                        _body_unit_count(html_filter.parse(cur_bytes).units) == bodyspec["n_units"]
                    )
                except Exception:
                    same_shape = False
                # Structure aligned -> use the TARGET as the skeleton, so a held-back
                # paragraph keeps its STORED TRANSLATION (taxonomy B). A fresh clone's
                # postBody IS the source, so this is also the correct first-fill path.
                skeleton = cur_bytes if same_shape else None
            if skeleton is None:
                if not complete:
                    # Real drift AND something held back: rebuilding from the source
                    # skeleton would write SOURCE text over the target's stored
                    # translations for the held-back units (Strapi H2). Omit the body.
                    held["postBody"] = (
                        "the target's HTML structure has drifted from the "
                        "source and some paragraphs are awaiting review - "
                        "body not written (nothing overwritten)"
                    )
                else:
                    # Everything IS translated, so no source text can survive into the
                    # target — a full replace is safe for taxonomy B. REVIEW LOW-7: it is
                    # NOT invisible, though. Rebuilding from the SOURCE skeleton discards
                    # any target-only human restructuring (an added paragraph, a reordered
                    # block). The behaviour is the deliberate asymmetry — we keep it — but
                    # the operator is now TOLD, because "your edits were superseded" is not
                    # something a person should have to diff for.
                    skeleton = source_bytes
                    if isinstance(current, str) and current:
                        # ROUND-2 LOW-7: this is a field that WAS written, so it must not
                        # go in `held` — `held_fields` means "not written" to every
                        # machine consumer (the retry path included). Own channel.
                        replaced["postBody"] = (
                            "the target's HTML structure had drifted from the source; "
                            "every paragraph was translated, so the body was REPLACED "
                            "wholesale from the source structure - any target-only edits "
                            "to that structure were superseded"
                        )
            if skeleton is not None and mapping:
                built = html_filter.build(skeleton, mapping).decode("utf-8")
                # On a target skeleton, replace only translated units with their
                # SOURCE text for the baseline. Held units keep the target's text.
                source_units = {
                    str(u.p_index): u.text
                    for u in html_filter.parse(source_bytes).units
                    if str(u.p_index) in mapping
                }
                baseline = html_filter.build(skeleton, source_units).decode("utf-8")
                if template_spans_match(baseline, built, _HUBL_SPAN_RE):
                    body["postBody"] = built
                    tokens.append(_prop_token("postBody"))
                else:
                    held["postBody"] = (
                        "HubSpot template integrity failed in assembled body: a span was "
                        "added, changed, or duplicated - not written; "
                        "preserve source spans and retry"
                    )
                    replaced.pop("postBody", None)
                    log.warning("hubspot: %s", held["postBody"])
        return body, tokens, held, replaced


def _body_unit_count(units) -> int:
    """Number of BODY units from the html filter, excluding its appended accessibility
    units (`a11y:*` style: image alt/title, link title/aria-label; `meta:*` page metadata,
    #733)."""
    from app.filters.a11y import is_appended_style

    return sum(1 for u in units if not is_appended_style(getattr(u, "style", "")))


class HubSpotConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="hubspot",
        topology=Topology.T3,  # T3 with NATIVE linkage (see the docstring)
        auth=(AuthKind.API_KEY,),  # Service Key / Private App token; OAuth 3LO deferred
        api_version=API_VERSION,
        rich_text=RichText.HTML,  # blog postBody rides the shared html filter
        regions="api.hubapi.com",  # single global host; no region field
        change_signals=ChangeSignals(
            webhook_sig=None,  # HubSpot publishes NO CMS webhooks (digest §4)
            poll_field="updatedAt",  # the ONLY signal; metadata-tainted (digest §4)
            stale_flag=None,  # HubSpot exposes no per-locale stale flag
            thin_payload=True,
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = HubSpotVariantStrategy()
        # Field-selection visibility (taxonomy D / digest §6.2): accumulated across
        # extract() calls, surfaced by build_message / build_result, logged once per name
        # for an allowlist miss so a custom module is never SILENTLY missed.
        self._sel_selected: set = set()
        self._sel_skipped: dict = {}
        self._sel_warned: set = set()
        # Per-run INCOMPLETE markers: {(record_id, locale): {field: reason}} — fields NOT
        # written. `_replaced` is the separate channel for fields that WERE written but
        # superseded target-side edits (round-2 LOW-7), so `held_fields` stays
        # single-meaning for machine consumers.
        self._incomplete: dict = {}
        self._replaced: dict = {}
        # Per-run tracking delta, persisted per successful pair by _hubspot_on_write_ok
        # BEFORE the end-of-run hash merge: {"<aid>:<loc>": (variant_aid, {tokens})}.
        self._written_delta: dict = {}
        # Per-run caches so a group's primary / an object's body is fetched once.
        self._obj_cache: dict = {}
        # Per-run operator-visible notes (review MEDIUM-3 / MEDIUM-5): named ids that were
        # re-resolved to a group primary, and objects skipped for being authored in a
        # language other than the connection's source. Both are surfaced by
        # build_message / build_result — neither is ever silent.
        self._substitutions: list = []
        self._lang_skipped: dict = {}
        # Objects matched on BASE language only (portal tags `en-us`, connection says
        # `en`): translated, but reported so an operator can tighten `source_language`.
        self._lang_regional: dict = {}

    # ------------------------------------------------------------ recording
    def tracking_key(self, record_id: str, locale: str) -> str:
        """The tracking key for one (object, language) pair — INCLUDING the content state
        (review HIGH-2).

        A state-less key was a live data-loss hole on the runbook's own MANDATORY-1 revert
        path: after flipping `content_state` from draft to published, the tracked paths
        were still non-empty in the NEW state (a language variation's other representation
        is the CLONE, i.e. pure source text), so the presence-only probe reported "covered"
        and every pair was suppressed forever — while the run message claimed translations
        had been written to the live object. Keying on the state makes a flip look exactly
        like what it is: untracked in that state, therefore a full re-write.

        The content-HASH stays state-less on purpose: it identifies the SOURCE content,
        which does not change when the operator flips which representation we write."""
        return f"{record_id}:{str(locale).lower()}:{self.content_state}"

    def _record_written(self, record_id, locale, variant_aid, tokens) -> None:
        if not tokens:
            return
        key = self.tracking_key(record_id, locale)
        vid, have = self._written_delta.get(key, (variant_aid, set()))
        self._written_delta[key] = (variant_aid or vid, set(have) | set(tokens))

    def _record_substitution(self, named, used, reason: str) -> None:
        self._substitutions.append(
            {"named": str(named), "translated": (str(used) if used else None), "reason": reason}
        )
        log.warning("hubspot: %s was named directly but %s", named, reason)

    def _record_incomplete(self, record_id, locale, held: dict) -> None:
        if not held:
            return
        self._incomplete.setdefault((str(record_id), str(locale)), {}).update(held)

    def _record_replaced(self, record_id, locale, replaced: dict) -> None:
        """Fields that WERE written but superseded target-side edits (round-2 LOW-7). Kept
        OFF `_incomplete` so `held_fields` keeps its single meaning ("not written") for
        every machine consumer, including the retry path."""
        if not replaced:
            return
        self._replaced.setdefault((str(record_id), str(locale)), {}).update(replaced)

    def tracked(self, record_id: str, locale: str):
        """(variant record-id, {path tokens}) previously recorded for this pair IN THE
        CURRENT CONTENT STATE, from the connection config. Empty when untracked -> the
        probe reports NOT covered (which is also what a content_state flip produces, by
        design — see tracking_key)."""
        key = self.tracking_key(record_id, locale)
        vid = (self.config.get(_VARIANTS_KEY) or {}).get(key)
        toks = set((self.config.get(_WRITTEN_KEY) or {}).get(key) or ())
        return (str(vid) if vid else None), toks

    # ------------------------------------------------------------ auth / http
    @property
    def content_state(self) -> str:
        """Which object state we read, PROBE and write (gotcha 2). One value drives all
        three, validated fail-closed so a typo can never silently split them."""
        raw = (self.config.get("content_state") or _STATE_DRAFT).strip().lower()
        if raw not in _STATES:
            raise ConnectorError(
                f"Unsupported HubSpot content state {raw!r}; use "
                f"{' or '.join(repr(s) for s in _STATES)}. This setting controls the "
                f"read, the existence probe AND the write together - they always match."
            )
        return raw

    def _token(self) -> str:
        token = (self.secret.get("access_token") or "").strip()
        if not token:
            raise ConnectorError("HubSpot Service Key (access token) is required.")
        return token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, *parts: str) -> str:
        """Build an API URL from ALREADY-VALIDATED parts. Every caller passes a segment
        from `_CONTENT_TYPES` (a module constant) and an id that went through
        `_validate_object_id`, so nothing caller-supplied is interpolated unchecked
        (taxonomy A). `_validate_target` is the defence-in-depth SSRF check on the
        fully-built https URL (repo law)."""
        url = "/".join([API_BASE] + [p.strip("/") for p in parts if p])
        base._validate_target(url)
        return url

    def _call(self, method: str, url: str, *, params=None, json_body=None, idempotent: bool = True):
        """One authed HubSpot call -> (status, parsed body). 429/5xx retry + Retry-After
        come from the SHARED base._request (repo law: no hand-rolled retry). A non-JSON
        body (HubSpot's proxy/edge layers return an HTML error page, digest §6.13) is
        turned into a clear ConnectorError rather than a crash."""
        with self._client() as c:
            r = self._request(
                c,
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(),
                idempotent=idempotent,
            )
        if r.status_code == 204 or not (r.content or b"").strip():
            return r.status_code, {}
        try:
            return r.status_code, r.json()
        except Exception as e:
            snippet = re.sub(r"<[^>]+>", " ", r.text or "")[:160].strip()
            raise ConnectorError(
                f"HubSpot returned a non-JSON response (HTTP {r.status_code}): "
                f"{snippet or '(empty)'}"
            ) from e

    @staticmethod
    def _err(status: int, body) -> str:
        msg = ""
        if isinstance(body, dict):
            msg = str(body.get("message") or body.get("error") or "")
        return f"HubSpot HTTP {status}: {vendor_quote(msg) or '(no message)'}"

    # -------------------------------------------------- validation (taxonomy A)
    def content_types(self) -> list[str]:
        """The configured content types, validated fail-closed against the SUPPORTED set.
        An operator config string is untrusted input too (the Strapi N4 lesson), so a
        deferred type gets its real reason and anything else is refused outright."""
        raw = (self.config.get("content_types") or "").strip()
        if not raw:
            return ["site-pages"]
        out: list[str] = []
        for x in _parse_names(raw):
            t = x.strip().lower()
            if t in _DEFERRED_TYPES:
                raise ConnectorError(
                    f"HubSpot {_DEFERRED_TYPES[t]} are NOT supported by this connector "
                    f"version. They have no native language link in HubSpot's API and "
                    f"their endpoints are HubSpot-beta, so they need a different "
                    f"(persisted-correlation) design that is deliberately not shipped "
                    f"here rather than half-built. Supported: "
                    f"{', '.join(_SUPPORTED_TYPES)}."
                )
            if t not in _CONTENT_TYPES:
                raise ConnectorError(
                    f"Unsupported HubSpot content type {x.strip()!r}; supported: "
                    f"{', '.join(_SUPPORTED_TYPES)}."
                )
            if t not in out:
                out.append(t)
        if not out:
            raise ConnectorError("At least one HubSpot content type is required.")
        return out

    def _validate_aid(self, raw, types: list[str] | None = None) -> tuple[str, str]:
        """Fail-closed scope + shape check for a caller-supplied record id (taxonomy A).

        Accepts `"<content-type>:<numeric id>"`, or a bare numeric id when the connection
        is configured for exactly ONE content type. The content-type half is checked
        against the connection's CONFIGURED types (not merely the supported ones — the
        Strapi H1 fix), and the id half must be purely numeric and bounded, so `..`,
        `%2e%2e%2f`, unicode, NUL, CR/LF, `?`, `#` and a type that is a PREFIX of a
        configured one are all refused BEFORE anything reaches a URL. Returns
        (content_type, object_id)."""
        types = types if types is not None else self.content_types()
        raw_s = str(raw or "")
        # Refuse control characters on the RAW value, BEFORE any strip: a trailing CR/LF
        # would otherwise be silently normalised away instead of surfacing the malformed
        # input (header/response-splitting shapes must fail loud, not be laundered).
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw_s):
            raise ConnectorError(
                f"Unsafe HubSpot object id {raw!r}: control characters are not allowed."
            )
        s = raw_s.strip()
        if not s:
            raise ConnectorError("A HubSpot object id is required.")
        if ":" in s:
            ctype, _, oid = s.partition(":")
            ctype = ctype.strip().lower()
            oid = oid.strip()
        elif len(types) == 1:
            ctype, oid = types[0], s
        else:
            raise ConnectorError(
                f"Ambiguous HubSpot object id {raw!r}: this connection is configured for "
                f"{len(types)} content types, so ids must be '<content-type>:<id>' "
                f"(one of {', '.join(types)})."
            )
        if ctype not in _CONTENT_TYPES or ctype not in types:
            raise ConnectorError(
                f"Object id {raw!r} names content type {ctype!r}, which is not one this "
                f"connection is configured for ({', '.join(types)}) - refusing to "
                f"target it."
            )
        if not _OBJECT_ID_RE.match(oid):
            raise ConnectorError(
                f"Unsafe HubSpot object id {raw!r}: the id must be numeric (1-20 digits)."
            )
        return ctype, oid

    @staticmethod
    def _validate_language(raw) -> str:
        """Language tags reach both a request BODY and (via the group dict) our own
        tracking keys, so they are allowlisted to a BCP-47-ish charset, fail-closed."""
        raw_v = str(raw or "")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw_v):
            raise ConnectorError(
                f"Unsafe HubSpot language tag {raw!r}: control characters are not allowed."
            )
        v = raw_v.strip()
        if not _LANG_RE.match(v):
            raise ConnectorError(
                f"Unsafe HubSpot language tag {raw!r}: use a language code such as 'fr', "
                f"'de' or 'es-mx'."
            )
        return v

    def _segment(self, ctype: str) -> str:
        return _CONTENT_TYPES[ctype]["segment"]

    def _body_kind(self, ctype: str) -> str:
        return _CONTENT_TYPES[ctype]["body"]

    @staticmethod
    def _aid(ctype: str, oid) -> str:
        return f"{ctype}:{oid}"

    def _object_url(self, ctype: str, oid: str) -> str:
        """The read/probe/write URL for ONE object in the configured state (gotcha 2).
        There is exactly ONE builder, so read, probe and write CANNOT diverge."""
        seg = self._segment(ctype)
        if self.content_state == _STATE_DRAFT:
            return self._url(seg, oid, "draft")
        return self._url(seg, oid)

    # ----------------------------------------------------- objects / linkage
    def fetch_object(self, record_id: str, *, use_cache: bool = True):
        """The object in the CONFIGURED state, or strategy.ABSENT when it does not exist
        there. Cached per run so the probe, the primary resolve and the write share one
        GET per object."""
        ctype, oid = self._validate_aid(record_id)
        key = (ctype, oid, self.content_state)
        if use_cache and key in self._obj_cache:
            return self._obj_cache[key]
        status, body = self._call("GET", self._object_url(ctype, oid))
        if status == 404:
            out = ABSENT
        elif status >= 400:
            raise ConnectorError(self._err(status, body))
        elif not isinstance(body, dict) or not body.get("id"):
            out = ABSENT
        else:
            out = body
        self._obj_cache[key] = out
        return out

    def resolve_primary(self, record_id: str):
        """The language group's TRUE PRIMARY record id (digest §6.5). If the object we
        were handed is itself a translation (`translatedFromId` set) we re-resolve to the
        primary UNCONDITIONALLY, so create-language-variation can never be aimed at a
        variant and produce a variation-of-a-variation. Bounded (a malformed group cannot
        loop). ABSENT when the object does not exist."""
        ctype, _oid = self._validate_aid(record_id)
        cur = record_id
        for _hop in range(4):
            obj = self.fetch_object(cur)
            if obj is ABSENT:
                return ABSENT
            parent = str(obj.get("translatedFromId") or "").strip()
            if not parent:
                return cur
            if not _OBJECT_ID_RE.match(parent):
                # A group pointer we cannot trust: stop here rather than build a URL
                # from it (taxonomy A applies to SERVER-supplied strings too).
                log.warning(
                    "hubspot: %s has a non-numeric translatedFromId %r; using it "
                    "as the group primary.",
                    cur,
                    parent[:40],
                )
                return cur
            cur = self._aid(ctype, parent)
        log.warning(
            "hubspot: language-group chain from %s did not reach a primary in 4 hops; using %s.",
            record_id,
            cur,
        )
        return cur

    def native_variant(self, primary_record_id: str, locale: str):
        """HubSpot's OWN correlation: the record id of the language variation for
        `locale`, read straight off the primary's `translations` dict (digest §3a), or
        None. This is the create-vs-reuse decision AND the phantom-clone recovery path —
        it is deliberately NOT the existence probe (gotcha 3)."""
        ctype, _oid = self._validate_aid(primary_record_id)
        obj = self.fetch_object(primary_record_id, use_cache=False)
        if obj is ABSENT:
            return None
        want = str(locale).strip().lower()
        for tag, entry in (obj.get("translations") or {}).items():
            if str(tag).strip().lower() != want:
                continue
            vid = str((entry or {}).get("id") or "").strip()
            if _OBJECT_ID_RE.match(vid):
                return self._aid(ctype, vid)
            log.warning(
                "hubspot: translations[%s] on %s has a non-numeric id %r; ignoring it.",
                want,
                primary_record_id,
                vid[:40],
            )
        return None

    def create_variation(self, primary_record_id: str, locale: str):
        """POST create-language-variation off the group's PRIMARY. HubSpot CLONES the
        primary's full content into a new object in DRAFT (digest §6.4) — so the object
        this returns is a phantom until our PATCH lands. idempotent=False: this CREATES
        server-side, so a 5xx/timeout must not be resent (base._request law).
        Returns (variant record-id | None, error)."""
        ctype, oid = self._validate_aid(primary_record_id)
        lang = self._validate_language(locale)
        req: dict = {"id": oid, "language": lang}
        if _CONTENT_TYPES[ctype]["body"] != "html":
            # REVIEW MEDIUM-5: primaryLanguage comes from the OBJECT, not the connection
            # config. Sending the configured source language for a primary HubSpot has
            # tagged differently is how you get a hard 400 (or, worse, a silently
            # mis-grouped variation). The config value is only the DISCOVERY filter.
            # #660: blog posts are the exception. HubSpot's blog-post schema
            # (BlogPostLanguageCloneRequestVNext) is id, language and usePublished only,
            # so primaryLanguage is not sent there (the group's primary is the cloned id).
            obj = self.fetch_object(primary_record_id)
            obj_lang = (
                str(obj.get("language") or "").strip()
                if obj is not ABSENT and isinstance(obj, dict)
                else ""
            )
            req["primaryLanguage"] = self._validate_language(obj_lang or self.source_language())
        url = self._url(self._segment(ctype), "multi-language", "create-language-variation")
        try:
            status, body = self._call(
                "POST",
                url,
                idempotent=False,
                json_body=req,
            )
        except ConnectorError as e:
            return None, str(e)
        if status >= 400:
            return None, self._err(status, body)
        vid = str((body or {}).get("id") or "").strip()
        if not _OBJECT_ID_RE.match(vid):
            return None, (f"create-language-variation returned no usable id ({vid[:40]!r})")
        # The new variant is not in any cache yet, and the primary's translations dict
        # just changed -> drop the primary's cached copy so a later read sees the link.
        self._obj_cache.pop((ctype, oid, self.content_state), None)
        return self._aid(ctype, vid), ""

    def patch_object(self, record_id: str, body: dict):
        """PATCH the object in the CONFIGURED state (gotcha 2 — the same state we read and
        probe). Never publishes: no push-live, no schedule call, ever. Returns
        (ok, error)."""
        ctype, oid = self._validate_aid(record_id)
        try:
            status, resp = self._call("PATCH", self._object_url(ctype, oid), json_body=body)
        except ConnectorError as e:
            return False, str(e)
        if status >= 400:
            return False, self._err(status, resp)
        self._obj_cache.pop((ctype, oid, self.content_state), None)
        return True, ""

    # ---------------------------------------------------------- source/locales
    def source_language(self) -> str:
        return (self.config.get("source_language") or "en").strip().lower() or "en"

    def enabled_locales(self) -> list[str]:
        """HubSpot has no per-portal "enabled languages" endpoint for this surface (the
        reference ships a static ~190-entry dropdown, digest §6.10), so we do NOT invent a
        preflight allowlist that would reject valid tags. Every syntactically valid target
        is usable; an unconfigured language surfaces as a real HubSpot error on
        create-language-variation, attributed to that (object, locale) pair rather than
        failing the run. The source language is excluded by the partition hook."""
        return []

    # ------------------------------------------------------------ test / list
    def test(self) -> tuple[bool, str]:
        try:
            types = self.content_types()
            state = self.content_state
            total = 0
            for t in types:
                status, body = self._call(
                    "GET", self._url(self._segment(t)), params={"limit": 1, "archived": "false"}
                )
                if status >= 400:
                    return False, self._err(status, body)
                total += int((body or {}).get("total") or 0)
            return True, (
                f"Connected to HubSpot ({', '.join(types)}; {total} object(s) "
                f"visible; writing the {state} state)."
            )
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        try:
            return [
                {"id": t, "name": f"{_CONTENT_TYPES[t]['label']}s", "tables": []}
                for t in self.content_types()
            ]
        except Exception:
            return []

    # --------------------------------------------------------------- discovery
    def _list_ids(self, ctype: str, cap: int) -> list[str]:
        """Cursor pagination over ONE content type (digest §5: `paging.next.after`),
        collecting ids only. The list endpoint has no `/draft` form, so it is used purely
        to ENUMERATE; every object is then re-fetched in the configured state
        (`discover_articles`), which is what keeps the source we EXTRACT and the target we
        WRITE in the same state (gotcha 2). A repeated or missing cursor ends the walk, so
        a misbehaving `paging` block cannot loop."""
        out: list[str] = []
        after = None
        page = min(100, max(1, cap))
        seen: set = set()
        while len(out) < cap:
            params = {"limit": page, "archived": "false"}
            if after:
                params["after"] = after
            status, body = self._call("GET", self._url(self._segment(ctype)), params=params)
            if status >= 400:
                raise ConnectorError(self._err(status, body))
            results = (body or {}).get("results") or []
            for item in results:
                oid = str((item or {}).get("id") or "").strip()
                if not _OBJECT_ID_RE.match(oid) or oid in seen:
                    continue
                # Skip objects that are themselves language variations: they are reached
                # through their group's primary, never translated as sources.
                if str((item or {}).get("translatedFromId") or "").strip():
                    continue
                seen.add(oid)
                out.append(oid)
            nxt = (((body or {}).get("paging") or {}).get("next") or {}).get("after")
            if not results or not nxt or nxt == after:
                break
            after = nxt
        return out[:cap]

    def _record(self, ctype: str, item: dict) -> dict:
        rec = dict(item or {})
        rec["_ctype"] = ctype
        rec["_aid"] = self._aid(ctype, str(item.get("id")))
        return rec

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source objects to translate. Explicit `article_ids` win and are SCOPE-VALIDATED
        (taxonomy A). Otherwise each configured content type is listed and the SHARED cap
        is filled ROUND-ROBIN across types, so the first type can never starve the rest
        (taxonomy E — assert DISTRIBUTION, not just the total).

        REVIEW MEDIUM-3 — an explicitly NAMED id that is itself a language variation is
        RE-RESOLVED to its group primary before anything else happens. `_list_ids` already
        skipped variations on the discovery path, but the caller paths (/api, the CLI's
        `--article-ids`, retry_failed, the runbook's own MANDATORY-2 steps) did not — and
        "a guard that covers most paths is the bug". Left unresolved, naming the fr
        variation relay-translated FROM the translation ("[de] [fr] Landing hero") and
        filed the tracking under the VARIANT's aid, leaving the primary's pair untracked
        forever. The substitution is REPORTED (never silent); a primary outside the
        configured scope is refused rather than reached."""
        cap = fetch_cap(cap or self.config.get("max_entries"), 50)
        types = self.content_types()
        if article_ids:
            out: list[dict] = []
            seen_aids: set = set()
            for raw in list(article_ids)[:cap]:
                ctype, oid = self._validate_aid(raw, types)
                aid = self._aid(ctype, oid)
                obj = self.fetch_object(aid)
                if obj is ABSENT or not isinstance(obj, dict):
                    continue
                if str(obj.get("translatedFromId") or "").strip():
                    primary = self.resolve_primary(aid)
                    if primary is ABSENT:
                        self._record_substitution(
                            aid,
                            None,
                            "it is a language variation whose group primary "
                            "could not be resolved - skipped",
                        )
                        continue
                    # The primary is always the same content type, so it is in scope by
                    # construction; re-validate anyway (fail-closed, taxonomy A).
                    p_ctype, _p_oid = self._validate_aid(primary, types)
                    p_obj = self.fetch_object(primary)
                    if p_obj is ABSENT or not isinstance(p_obj, dict):
                        self._record_substitution(
                            aid,
                            primary,
                            "its group primary is not readable in the "
                            f"'{self.content_state}' state - skipped",
                        )
                        continue
                    self._record_substitution(
                        aid,
                        primary,
                        "it is a language variation; translating its group primary instead",
                    )
                    ctype, aid, obj = p_ctype, primary, p_obj
                if aid in seen_aids:
                    continue  # two variations of one group collapse to one primary
                seen_aids.add(aid)
                self._charge_fetch(item_chars(obj))  # #587: full objects stay buffered
                out.append(self._record(ctype, obj))
            return out
        per_type = [(t, self._list_ids(t, cap)) for t in types]
        picked: list[tuple[str, str]] = []
        i = 0
        while len(picked) < cap and any(i < len(ids) for _t, ids in per_type):
            for t, ids in per_type:
                if i < len(ids):
                    picked.append((t, ids[i]))
                    if len(picked) >= cap:
                        break
            i += 1
        records: list[dict] = []
        for ctype, oid in picked:
            obj = self.fetch_object(self._aid(ctype, oid))
            if obj is not ABSENT and isinstance(obj, dict):
                self._charge_fetch(item_chars(obj))  # #587
                records.append(self._record(ctype, obj))
        return records

    # ------------------------------------------------------------- selection
    def _meta_enabled(self) -> bool:
        return str(self.config.get("translate_meta", "1")).strip().lower() not in (
            "0",
            "false",
            "no",
        )

    def _selectable(self, name: str, value: str) -> tuple[bool, str]:
        """(select?, skip reason) for one module-property LEAF — gated on BOTH the
        property NAME and the VALUE (review HIGH-1).

        The NAME gate: `_NEVER_TRANSLATE` identity/machine names always win; then an
        operator `fields` allowlist (if set) replaces the default name set, else the
        `_TEXT_PROPERTIES` allowlist minus `exclude_fields`.

        The VALUE gate (`_value_is_machine`) runs in BOTH modes and is the fix for the
        reviewer's probe: a HubL `module_block`, a `{{ page.name }}` expression, a CDN URL
        and a contact email all sit under ordinary names like `text`/`title`/`content`/
        `label` and previously reached MT and the wire. See the `_value_is_machine`
        comment for why the allowlist does NOT bypass it."""
        low = str(name).strip().lower()
        if low in _NEVER_TRANSLATE or low.endswith(_NEVER_SUFFIXES):
            return False, "identity/machine property (never translated)"
        allow = _parse_names(self.config.get("fields"))
        if allow:
            if low not in {a.lower() for a in allow}:
                return False, "not in the configured `fields` allowlist"
        elif low in {e.lower() for e in _parse_names(self.config.get("exclude_fields"))}:
            return False, "operator exclude_fields"
        elif low not in _TEXT_PROPERTIES:
            return False, (
                "property name is not in the translatable-property allowlist "
                "(add it to `fields` if this module property holds copy)"
            )
        mach = _value_is_machine(value)
        if mach:
            return False, mach
        return True, ""

    def _record_selection(self, selected, skipped: dict) -> None:
        self._sel_selected.update(selected)
        for name, reason in skipped.items():
            self._sel_skipped.setdefault(name, reason)
            if "allowlist" in reason and name not in self._sel_warned:
                self._sel_warned.add(name)
                log.warning(
                    "hubspot: module property %r holds text but is NOT in the "
                    "translatable-property allowlist, so it is NOT being "
                    "translated. Add it to the connection's `fields` setting if "
                    "it should be.",
                    name,
                )

    def _collect_layout_units(self, layout) -> list[tuple[list, str, str]]:
        """Walk the `layoutSections` module tree and return
        [(path, property_name, text)] for every SELECTED translatable leaf, in a stable
        depth-first order.

        REPORTING (review HIGH-1): EVERY unselected non-empty string leaf that contains a
        letter is recorded, deduped by property name. The previous prose heuristic
        (>=12 chars AND contains a space) excluded the single most common custom-module
        string class — `cta_label="Subscribe"`, `banner_copy="Learn more"`,
        `hero_sub="Fast"` — so those were skipped AND invisible (`fields_skipped: {}`).
        The letter test is the honest one: if a human could read it, the operator gets
        told it was not translated (taxonomy D / digest §6.2 — the reference's defect)."""
        selected: list[tuple[list, str, str]] = []
        picked: set = set()
        skipped: dict = {}

        def walk(node, path: list, key: str | None) -> None:
            if isinstance(node, dict):
                for k in sorted(node.keys()):  # sorted -> stable paths across runs
                    walk(node[k], path + [k], k)  # noqa: RUF005
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    walk(item, path + [i], key)  # noqa: RUF005
            elif isinstance(node, str):
                if not node.strip() or key is None:
                    return
                ok, reason = self._selectable(key, node)
                if ok:
                    selected.append((path, key, node))
                    picked.add(key)
                else:
                    # ROUND-2 LOW-5: report EVERY unselected non-empty leaf, including the
                    # letter-free ones. "24/7", "100%", "9:00", "2026-01-01" are genuinely
                    # localizable in many locales; the letter test belongs to SELECTION
                    # (we do not MT them by default) but gating the REPORT on it made the
                    # "no letters in the value" reason unreachable and hid them entirely.
                    skipped.setdefault(key, reason)

        walk(layout if isinstance(layout, (dict, list)) else {}, [], None)
        self._record_selection(picked, skipped)
        return selected

    def _selection_summary(self) -> str:
        if not self._sel_selected and not self._sel_skipped:
            return ""
        parts = [
            f"Translated module propert(ies): {', '.join(sorted(self._sel_selected)) or '(none)'}."
        ]
        if self._sel_skipped:

            def _rank(item):
                name, reason = item
                # ACTIONABLE first (a name the operator can allowlist), then the
                # value-shape rejects (informative), then the expected machine names —
                # so the cap-8 window always shows what a human can act on.
                if "allowlist" in reason:
                    tier = 0
                elif "identity/machine" in reason or "exclude_fields" in reason:
                    tier = 2
                else:
                    tier = 1
                return (tier, name)

            items = sorted(self._sel_skipped.items(), key=_rank)
            shown = "; ".join(f"{n} ({r})" for n, r in items[:8])
            more = "" if len(items) <= 8 else f"; +{len(items) - 8} more"
            parts.append(f"Skipped text propert(ies): {shown}{more}.")
        return " ".join(parts)

    # --------------------------------------------------------------- extract
    def _prepare_value(self, key: str, name: str, value: str, masks: dict, skipped: dict):
        """Gate ONE outgoing string and mask its HubL, or return None to drop it.

        Round-2 HIGH-1: `_value_is_machine` previously had a single call site
        (layoutSections), so `name`, `htmlTitle`, `metaDescription` and every postBody
        paragraph went out UNGATED — and a HubL SEO-title template is a documented HubSpot
        feature, i.e. the LIKELIER carrier. This is the one funnel every outgoing string
        now passes through, so the gate cannot have a hole again.

        Round-2 MEDIUM-4: an entirely-expression value is dropped (and reported); a MIXED
        HubL+prose value is MASKED and translated. `masks[key]` records the spans for
        `unmask_hubl` on the way back."""
        mach = _value_is_machine(value)
        if mach:
            skipped[name] = mach
            return None
        masked, spans = mask_hubl(value)
        if spans:
            masks[key] = spans
        return masked

    def extract(self, record: dict):
        """(title_text, body_units, chash, extra) for one source object.

        title_text is `name` (Segment order 0). body_units carry a p_index that IS the
        reinsertion address: "m:<prop>" for htmlTitle/metaDescription, "b<i>" for the
        i-th selected layoutSections leaf, or the html filter's own int p_index for a blog
        post's postBody.

        EVERY outgoing string — title, meta, module leaf and postBody paragraph — goes
        through `_prepare_value`, so the value gate and the HubL masking apply uniformly
        (round-2 HIGH-1 / MEDIUM-4). The content HASH is computed over the ORIGINAL,
        UNMASKED source, so a change confined to a HubL token still re-translates.

        A held title is the one value with no unit: `name` is Segment order 0, and the
        flow reads it from `title_text`. When the title is dropped by the gate we hand the
        flow an EMPTY title_text, and `build_payload` omits `name` from the patch — the
        same never-source-fill discipline as a NEEDS_REVIEW title (taxonomy B)."""
        ctype = record.get("_ctype") or ""
        raw_title = str(record.get("name") or "")
        units: list[_Unit] = []
        hash_parts: list[str] = [raw_title]
        masks: dict = {}
        skipped: dict = {}
        extra: dict = {"ctype": ctype, "paths": [], "meta_props": [], "masks": masks}

        title_text = ""
        if raw_title.strip():
            prepared = self._prepare_value("title", "name", raw_title, masks, skipped)
            if prepared is not None:
                title_text = prepared
            else:
                extra["title_dropped"] = True

        if self._meta_enabled():
            for prop in ("htmlTitle", "metaDescription"):
                val = record.get(prop)
                if not (isinstance(val, str) and val.strip()):
                    continue
                hash_parts.append(f"{prop}={val}")
                key = f"m:{prop}"
                prepared = self._prepare_value(key, prop, val, masks, skipped)
                if prepared is None:
                    continue
                units.append(_Unit(prepared, key))
                extra["meta_props"].append(prop)

        if self._body_kind(ctype) == "layout":
            # _collect_layout_units already ran the gate per leaf (it needs the NAME gate
            # too); masking is applied here so `masks` stays one dict.
            leaves = self._collect_layout_units(record.get("layoutSections") or {})
            for i, (path, _name, text) in enumerate(leaves):
                key = f"b{i}"
                masked, spans = mask_hubl(text)
                if spans:
                    masks[key] = spans
                units.append(_Unit(masked, key))
                extra["paths"].append(path)
                hash_parts.append(text)
            extra["body_kind"] = "layout"
        else:
            from app.filters.html_filter import HtmlFilter

            html_filter = HtmlFilter(template_pattern=_HUBL_SPAN_RE)
            post_body = str(record.get("postBody") or "")
            source_bytes = post_body.encode("utf-8")
            html_units = html_filter.parse(source_bytes).units if post_body else []
            for u in html_units:
                key = str(u.p_index)
                # Round-2 HIGH-1: postBody paragraphs carried HubL through per-paragraph
                # translation. Gate + mask them like every other string. A paragraph that
                # is ENTIRELY an expression is dropped; the html filter's skeleton then
                # keeps its ORIGINAL text for that p_index (no source-over-translation
                # risk: the target skeleton is the target's own current text).
                prepared = self._prepare_value(key, "postBody", u.text, masks, skipped)
                if prepared is None:
                    continue
                units.append(_Unit(prepared, u.p_index))
            hash_parts.append(post_body)
            extra.update(
                {
                    "body_kind": "html",
                    "filter": html_filter,
                    "source_bytes": source_bytes,
                    "n_units": _body_unit_count(html_units),
                }
            )

        if skipped:
            self._record_selection((), skipped)
        return title_text, units, _content_hash(*hash_parts), extra

    # ------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        out: list[PulledItem] = []
        for rec in self.discover_articles():
            title, units, _chash, _extra = self.extract(rec)
            aid = rec["_aid"]
            label = (title or aid)[:60]
            out.append(PulledItem(external_id=aid, field="name", text=title, title=label))
            for u in units:
                out.append(
                    PulledItem(external_id=aid, field=str(u.p_index), text=u.text, title=label)
                )
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("HubSpot writes per-language variations - use 'Translate now'.")

    def detect_change(self, conn=None):
        return []


# ===========================================================================
# Translate flow — thin hooks over the shared engine (flow.run_translate_flow).
# Poll-only (HubSpot has NO CMS webhooks, digest §4) and NO watermark: HubSpot's
# `updatedAt` is metadata-tainted with no documented content-only signal (digest
# §4), so the content-hash gate is the sole change detector.
# ===========================================================================
def _hubspot_preflight(ctx):
    conn = ctx.connector
    conn.content_types()  # validate operator config loudly, before any write
    _ = conn.content_state
    src = conn.source_language()
    # No enabled-locale probe exists on this surface (see enabled_locales), so every
    # requested locale is "enabled" and the partition hook drops the source language.
    return set(ctx.locales) | {src}, {"source": src}


def _hubspot_partition(locales, enabled, extra):
    """usable = every requested target except the SOURCE language; rejected = anything
    that is not a valid HubSpot language tag (refused loudly rather than sent to the
    wire). The source language is UNCONDITIONALLY dropped so a run can never create a
    "translation" of a page into its own language."""
    src = (extra or {}).get("source", "")
    rejected: list = []
    usable: list = []
    for loc in locales:
        if not _LANG_RE.match(str(loc).strip()):
            rejected.append(loc)
        elif str(loc).strip().lower() != str(src).strip().lower():
            usable.append(loc)
    return rejected, usable


def _language_match(obj_lang: str, src: str) -> tuple[bool, bool]:
    """(is the object in the source language?, does the REGION differ?).

    Round-2 ergonomic note: HubSpot portals commonly tag content `en-us` while the
    connection's default `source_language` is `en`. Exact-match-only turned that into a
    green all-skipped first run — technically honest, operationally a dead end. Matching is
    therefore: EXACT (case-insensitive) first, else BASE-LANGUAGE (`en` ~ `en-us`), which
    is a match but is REPORTED so the operator can tighten `source_language` if they meant
    to be strict. A different base language is never a match. An object with NO language
    tag is treated as the source (HubSpot omits it on single-language portals)."""
    o = str(obj_lang or "").strip().lower()
    s = str(src or "").strip().lower()
    if not o or o == s:
        return True, False
    if o.split("-")[0] == s.split("-")[0]:
        return True, True
    return False, False


def _hubspot_prepare(ctx, art):
    conn = ctx.connector
    # REVIEW MEDIUM-5 — translate only objects HubSpot itself tags with the connection's
    # SOURCE language. Without this a German-authored primary was handed to MT as if it
    # were English (the prompt asserts the source language), and every own-language target
    # produced a hard 400 from create-language-variation — with ok=True throughout.
    # Skip-and-REPORT via the flow's own counter, never a silent drop.
    src = conn.source_language()
    obj_lang = str(art.get("language") or "").strip().lower()
    aid = art.get("_aid")
    match, note = _language_match(obj_lang, src)
    if not match:
        conn._lang_skipped[str(aid)] = obj_lang
        return flow.SkipArticle(
            "language_skipped",
            error=f"{aid}: authored in {obj_lang!r}, not the connection's source language "
            f"{src!r} - skipped (set source_language, or use a separate connection "
            f"for that language)",
        )
    if note:
        conn._lang_regional[str(aid)] = obj_lang
    title_text, units, chash, extra = conn.extract(art)
    if not title_text.strip() and not units:
        return flow.SkipArticle("empty_skipped")
    aid = art["_aid"]
    extra["connector"] = conn
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text=title_text,
        body_units=units,
        extra_skeleton={
            "article_id": aid,
            "title": title_text or None,
            "content_type": art.get("_ctype"),
        },
        doc_title=f"{(title_text or art.get('_ctype') or 'page')[:50]} ({aid})",
        extra=extra,
    )


def _hubspot_is_suppressed(connector, aid, loc):
    return connector.strategy.exists_target(connector, aid, loc)


def _hubspot_on_write_ok(ctx, prep, loc):
    """GOTCHA 3: persist THIS pair's tracking NOW — per successful pair, BEFORE the
    end-of-run hash merge (flow stage 10) — so a crash can NEVER leave a suppression hash
    without the tracking that probe depends on (the Shopify H1 ordering). The variant id
    is an `add` and the written paths are a `union`, both in ONE locked op, so a
    concurrent run's delta for the same pair survives."""
    from app.services.connectors import store

    # The key carries the content state (HIGH-2) — one builder, so the write-side delta
    # and the read-side probe can never key differently.
    key = ctx.connector.tracking_key(prep.aid, loc)
    entry = (getattr(ctx.connector, "_written_delta", {}) or {}).get(key)
    if not entry:
        return
    variant_aid, tokens = entry
    changes: dict = {}
    if variant_aid:
        changes[_VARIANTS_KEY] = {"add": {key: variant_aid}}
    if tokens:
        changes[_WRITTEN_KEY] = {"union": {key: sorted(tokens)}}
    if changes:
        store.patch_config_nested(ctx.cid, changes)


def _hubspot_build_payload(prep, loc, seg0_target, mapping):
    """The write payload for one locale.

    TAXONOMY B, per field: a HELD-BACK (NEEDS_REVIEW) field is OMITTED entirely — the
    title is NEVER source-filled (that is Strapi H2 exactly), a meta property is simply
    absent, and a layout path is not in `sets`. The strategy merges into the TARGET's
    current content, so every omitted field keeps its stored translation. When NOTHING
    survives, the payload is empty and the strategy writes nothing at all (taxonomy F).

    HUBL RESTORATION (round-2 MEDIUM-4): every translated value whose source carried HubL
    is unmasked here. A token that did NOT survive translation byte-exact makes the field
    behave exactly like a held-back one — OMITTED and reported — because a half-restored
    template is a page that no longer renders."""
    conn = prep.extra["connector"]
    masks = prep.extra.get("masks") or {}
    props: dict = {}
    held: dict = {}
    sources = {str(u.p_index): u.text for u in prep.body_units}
    sources["title"] = prep.title_text

    def _restore(key, value):
        """(restored, reason|None). None reason = usable."""
        from app.services.segment import ph_point

        spans = masks.get(key) or []
        source = sources.get(key, "")
        for i, span in enumerate(spans, start=1):
            source = source.replace(ph_point("h", i), span)
        out, ok = unmask_hubl(value, spans, source)
        if ok:
            return out, None
        log.warning(
            "hubspot: template integrity failed for %s/%s field %s; not written",
            prep.aid,
            loc,
            key,
        )
        return None, (
            "HubSpot template integrity failed: a token did not survive translation intact "
            "or a template span was added, changed, or duplicated ({{ }} / {% %} / {# #}) "
            "- not written; preserve the source template spans and retry"
        )

    if prep.extra.get("title_dropped"):
        held["name"] = (
            "the source title is a HubSpot template expression with no "
            "translatable text - not translated"
        )
    elif seg0_target is not None:
        val, reason = _restore("title", seg0_target)
        if reason:
            held["name"] = reason
        else:
            props["name"] = val
    else:
        held["name"] = "awaiting review (NEEDS_REVIEW)"

    for prop in prep.extra.get("meta_props") or ():
        key = f"m:{prop}"
        if key not in mapping:
            held[prop] = "awaiting review (NEEDS_REVIEW)"
            continue
        val, reason = _restore(key, mapping[key])
        if reason:
            held[prop] = reason
        else:
            props[prop] = val

    body = None
    if prep.extra.get("body_kind") == "layout":
        sets = []
        protected = {}
        for i, path in enumerate(prep.extra.get("paths") or ()):
            key = f"b{i}"
            if key not in mapping:
                held[_path_token(path)] = "awaiting review (NEEDS_REVIEW)"
                continue
            val, reason = _restore(key, mapping[key])
            if reason:
                held[_path_token(path)] = reason
            else:
                sets.append((path, val))
                protected[_path_token(path)] = tuple(masks.get(key) or ())
        if sets:
            body = {"kind": "layout", "sets": sets, "protected": protected}
    else:
        n_units = prep.extra.get("n_units") or 0
        # `n_translatable` counts the paragraphs we actually SENT — a paragraph the value
        # gate dropped (entirely a HubL expression) is not "awaiting review", and counting
        # it as such would wrongly mark every such post permanently incomplete. On the
        # source-skeleton path a dropped paragraph correctly keeps its SOURCE text (an
        # expression translates to itself); on the target-skeleton path it keeps the
        # target's own current text.
        n_translatable = len([u for u in prep.body_units if not str(u.p_index).startswith("m:")])
        html_map: dict = {}
        for k, v in mapping.items():
            key = str(k)
            if key.startswith("m:"):
                continue
            val, reason = _restore(key, v)
            if reason:
                held[f"postBody paragraph {key}"] = reason
                continue
            html_map[key] = val
        missing = n_translatable - len(html_map)
        if missing > 0:
            held["postBody"] = (
                f"{missing} of {n_translatable} translatable paragraph(s) "
                f"were held back (awaiting review, or a template token did "
                f"not survive)"
            )
        if html_map:
            body = {
                "kind": "html",
                "filter": prep.extra["filter"],
                "source_bytes": prep.extra["source_bytes"],
                "n_units": n_units,
                "mapping": html_map,
                "complete": bool(n_translatable) and len(html_map) >= n_translatable,
            }

    # Operator-visible INCOMPLETE marker — only on a PARTIAL write (something survives).
    # An all-held payload stays the taxonomy-F held_back path, so it is never double
    # counted as both held-back and incomplete.
    if (props or body) and held:
        conn._record_incomplete(prep.aid, loc, held)
    return {"props": props, "body": body, "held_reasons": held}


def _hubspot_variant_exists(connector, aid, loc):
    """#640: does HubSpot already have the locale variation (its own linkage, not our
    tracking)? A NEW variation is a clone of the English source, so creating it while a unit
    is held would leave that unit in English; the flow holds the pair instead."""
    primary = connector.resolve_primary(aid)
    return primary is not ABSENT and connector.native_variant(primary, loc) is not None


def _is_held_back(f):
    return str((f or {}).get("error", "")).startswith(_HELD_BACK_PREFIX)


def _held_back(ctx):
    return [f for f in ctx.failures if _is_held_back(f)]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_held_back(f)]


def _dropped_as_source(ctx):
    return [l for l in ctx.locales if l not in ctx.usable and l not in ctx.rejected]  # noqa: E741


def _token_label(token: str) -> str:
    """A human label for a held-field key (review LOW-8). A raw
    `body:["main","widgets","w1","body","html"]` token is unreadable in an operator
    message and — being a PATH, not a property name — must not be phrased as something to
    add to `fields`. Rendered as `main > widgets > w1 > body > html` instead. Plain field
    names (`name`, `postBody`) and `prop:` tokens pass through unchanged."""
    if token.startswith("prop:"):
        return token[5:]
    if token.startswith("body:"):
        try:
            path = json.loads(token[5:])
        except (TypeError, ValueError):
            return token
        return " > ".join(str(p) for p in path)
    return token


def _replaced_pairs(ctx):
    """(object, locale) pairs whose body was WRITTEN but rebuilt from the source skeleton,
    superseding target-side structural edits (round-2 LOW-7). A SEPARATE channel from
    `incomplete`: these fields were written, so they must never appear in `held_fields`
    (which the retry path and every other machine consumer reads as "not written")."""
    failed = {(str(f.get("article_id")), str(f.get("locale"))) for f in ctx.failures}
    out = []
    for (aid, loc), rep in (getattr(ctx.connector, "_replaced", {}) or {}).items():
        if (aid, loc) in failed:
            continue
        out.append(
            {
                "article_id": aid,
                "locale": loc,
                "replaced_fields": sorted(_token_label(k) for k in rep),
                "reasons": dict(rep),
            }
        )
    return out


def _confirmed_incomplete(ctx):
    """(object, locale) pairs PARTIALLY written this run — some fields landed, others were
    held back or no longer exist in the target — CONFIRMED against the run's real failures
    (a pair whose write FAILED is a failure, not an incomplete). Operator-visible.

    A held-back field was never written, so the full-coverage probe treats the pair as
    covered and it is NOT auto-re-attempted: recovery is an explicit retry (which passes the
    pair as only_pairs, and the flow bypasses suppression for it) or a source edit (which
    changes the content hash)."""
    failed = {(str(f.get("article_id")), str(f.get("locale"))) for f in ctx.failures}
    out = []
    for (aid, loc), held in (getattr(ctx.connector, "_incomplete", {}) or {}).items():
        if (aid, loc) in failed:
            continue
        out.append(
            {
                "article_id": aid,
                "locale": loc,
                # Human labels (LOW-8); `reasons` keeps the raw tokens for machines.
                "held_fields": sorted(_token_label(k) for k in held),
                "reasons": dict(held),
            }
        )
    return out


def _hubspot_compute_ok(ctx):
    if not ctx.usable:
        return False
    real = _real_failures(ctx)
    return bool(
        (ctx.created + ctx.updated) > 0
        or (ctx.skipped > 0 and not real)
        or (not real and (ctx.created + ctx.updated + ctx.skipped) == 0)
    )


def _hubspot_build_message(ctx):
    state = "draft" if _hubspot_state(ctx) == _STATE_DRAFT else "published"
    parts = [
        f"Created {ctx.created} new, updated {ctx.updated} HubSpot language "
        f"variation(s) across {len(ctx.usable)} language(s) from "
        f"{len(ctx.articles)} object(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) language(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} object(s) with no translatable content."
        )
    if ctx.counters["language_skipped"]:
        langs = sorted(set((getattr(ctx.connector, "_lang_skipped", {}) or {}).values()))
        parts.append(
            f"Skipped {ctx.counters['language_skipped']} object(s) NOT authored "
            f"in this connection's source language "
            f"{ctx.connector.source_language()!r} (found: "
            f"{', '.join(langs) or 'other'}) - they were not translated."
        )
    regional = getattr(ctx.connector, "_lang_regional", {}) or {}
    if regional:
        tags = sorted(set(regional.values()))
        parts.append(
            f"Note: {len(regional)} object(s) are tagged "
            f"{', '.join(tags)} while this connection's source language is "
            f"{ctx.connector.source_language()!r} - matched on the base language "
            f"and translated. Set source_language to the exact tag if you want "
            f"strict matching."
        )
    subs = getattr(ctx.connector, "_substitutions", []) or []
    if subs:
        sample = "; ".join(f"{s['named']} -> {s['translated'] or 'skipped'}" for s in subs[:3])
        parts.append(
            f"Re-resolved {len(subs)} named id(s) that are language variations "
            f"to their group primary: {sample}."
        )
    if ctx.rejected:
        parts.append(
            f"Rejected {len(ctx.rejected)} invalid language tag(s): "
            f"{', '.join(str(r) for r in ctx.rejected)}."
        )
    dropped = _dropped_as_source(ctx)
    if dropped:
        parts.append(
            f"Skipped {len(dropped)} target(s) equal to the source language "
            f"({', '.join(dropped)}) - configure different target languages."
        )
    held = _held_back(ctx)
    if held:
        parts.append(
            f"Held back {len(held)} (object, language) pair(s) awaiting review "
            f"(NEEDS_REVIEW) - nothing written; they will be re-attempted next "
            f"run."
        )
        reasons = sorted({f.get("error", "") for f in held})
        parts.append("Held reasons: " + "; ".join(reasons[:3]))
    repl = _replaced_pairs(ctx)
    if repl:
        sample = "; ".join(
            f"{p['article_id']}/{p['locale']}: {', '.join(p['replaced_fields'])}" for p in repl[:3]
        )
        parts.append(
            f"Replaced the body structure of {len(repl)} pair(s) from the source "
            f"skeleton (the target had drifted and every paragraph was "
            f"translated) - any target-only structural edits were superseded: "
            f"{sample}."
        )
    incomplete = _confirmed_incomplete(ctx)
    if incomplete:
        sample = "; ".join(
            f"{p['article_id']}/{p['locale']}: {', '.join(p['held_fields'][:3])}"
            for p in incomplete[:3]
        )
        parts.append(
            f"Wrote {len(incomplete)} pair(s) INCOMPLETE - some field(s) were "
            f"held back or no longer exist in the target, the rest were "
            f"written: {sample}. A held-back field needs an explicit RETRY (a "
            f"plain re-run skips it - the pair is covered); a source edit also "
            f"brings it back."
        )
        reasons = sorted({r for p in incomplete for r in p["reasons"].values()})
        parts.append("Held field reasons: " + "; ".join(reasons[:3]))
    if state == _STATE_DRAFT:
        parts.append(
            "Note: translations were written to each variation's DRAFT and are "
            "NOT live. Review them in HubSpot and push the draft live when you "
            "are happy."
        )
    else:
        parts.append(
            "Note: this connection is set to 'published' - translations were "
            "written to the live object, with no draft review step."
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
        parts.append(ctx.budget.message("variation write(s)").strip())
        log.warning(
            "hubspot_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _hubspot_state(ctx) -> str:
    try:
        return ctx.connector.content_state
    except ConnectorError:  # pragma: no cover — preflight already refused it
        return _STATE_DRAFT


def _hubspot_build_result(ctx):
    # NOTE: the tracking maps are persisted per-pair in _hubspot_on_write_ok (BEFORE the
    # hash merge), never here — build_result runs AFTER the hash, which would leave a
    # crash window of hash-without-tracking (Shopify H1).
    return {
        "objects": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "held_back": len(_held_back(ctx)),
        "empty_skipped": ctx.counters["empty_skipped"],
        "language_skipped": ctx.counters["language_skipped"],
        "language_skipped_objects": dict(getattr(ctx.connector, "_lang_skipped", {})),
        "language_regional": dict(getattr(ctx.connector, "_lang_regional", {})),
        "substitutions": list(getattr(ctx.connector, "_substitutions", [])),
        "replaced": _replaced_pairs(ctx),
        "rejected": ctx.rejected,
        "dropped_source": _dropped_as_source(ctx),
        "content_state": _hubspot_state(ctx),
        "incomplete": _confirmed_incomplete(ctx),
        "fields_selected": sorted(ctx.connector._sel_selected),
        "fields_skipped": dict(ctx.connector._sel_skipped),
        "failures": _real_failures(ctx),
    }


def hubspot_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate HubSpot site pages / landing pages / blog posts into one or more
    languages, each written as a real HubSpot LANGUAGE VARIATION linked into the object's
    own multi-language group (T3 with native linkage — HubSpot owns the source/target
    correlation, we persist no id map).

    Thin wrapper over flow.run_translate_flow; poll-only with content-hash suppression
    gated on OUR OWN registered-set probe (a variation's mere existence never counts as
    translated — it is a full clone of the source until our write lands). Writes to each
    variation's DRAFT by default and never publishes. A partial write is surfaced as an
    operator-visible incomplete pair; a held-back field is NOT auto-re-attempted (the pair
    is covered) — recovery is retry_failed (which passes the pair as only_pairs, and the flow
    bypasses suppression for it) or a source edit. `only_pairs` restricts the write loop to
    exactly those (object, language) pairs and bypasses content-hash suppression for them (an
    explicit operator retry). Marketing emails and marketing forms are NOT supported by this
    version."""
    spec = flow.TranslateFlowSpec(
        kind="hubspot",
        no_conn_msg="No such HubSpot connection.",
        hash_key="hubspot_hashes",
        probe_label="HubSpot",
        preflight=_hubspot_preflight,
        prepare=_hubspot_prepare,
        is_suppressed=_hubspot_is_suppressed,
        build_payload=_hubspot_build_payload,
        compute_draft=lambda cfg: True,
        compute_ok=_hubspot_compute_ok,
        build_message=_hubspot_build_message,
        build_result=_hubspot_build_result,
        held_prefix=_HELD_BACK_PREFIX,
        report_held_updates=False,
        variant_exists=_hubspot_variant_exists,
        on_write_ok=_hubspot_on_write_ok,
        extra_counters=("empty_skipped", "language_skipped"),
        partition=_hubspot_partition,
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
