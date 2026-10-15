"""jira: part1

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
import re

from sqlmodel import Session, select

from app import config
from app.db import engine
from app.filters import adf
from app.models import Document, Project, Segment, TransMode
from app.services import providers
from app.services.connectors import egress_guard
from app.services.connectors.base import (
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    harvest_context_field,
)

log = logging.getLogger("loc_tms.connectors.jira")


SPEC = ConnectorSpec(
    kind="jira",
    name="Jira",
    icon="🧩",
    cat="Issue tracking",
    blurb="Pull issue summaries/descriptions via JQL, translate them, and write each "
    "translation back as a comment (or into a custom field) on its issue.",
    config_fields=[
        FieldSpec(
            "base_url",
            "Site URL",
            "Your Jira Cloud site.",
            placeholder="https://your-team.atlassian.net",
        ),
        FieldSpec(
            "jql",
            "JQL",
            "Which issues to pull. Also the declared SCOPE: a push's "
            "issue key and any caller-supplied JQL are ANDed against this, so a request "
            "can never translate outside it. Blank = any issue the token can read. "
            "Only an admin can change it once the connection holds a saved credential.",
            placeholder="project = LOC AND status = 'To Localize'",
        ),
        FieldSpec(
            "source_field",
            "Source field(s)",
            "Field(s) to translate, comma-separated "
            "(e.g. summary,description). Custom fields by id are validated as text-typed.",
            required=False,
            placeholder="summary,description",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "Language of the source field.",
            required=False,
            placeholder="en-US",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated languages to translate into.",
            required=False,
            placeholder="es-MX, de-DE, fr-FR",
        ),
        FieldSpec(
            "target_locale",
            "Target locale",
            "Single target (used if Target locales is blank).",
            required=False,
            placeholder="fr-FR",
        ),
        FieldSpec(
            "target_mode",
            "Write back as",
            "'comment' (default, one per language, "
            "non-destructive) or 'field': writes the translation into the Custom field id "
            "below, one language into one target custom field. The source summary and "
            "description are never overwritten, and field mode refuses to run without a "
            "target field. Use comment mode for several languages.",
            required=False,
            placeholder="comment",
        ),
        FieldSpec(
            "target_field",
            "Custom field id",
            "Required in field mode: the custom field that receives the translation "
            "(it must not be one of the source fields).",
            required=False,
            placeholder="customfield_10050",
        ),
        FieldSpec(
            "translate_comments",
            "Translate existing comments",
            "Off by default. When on, "
            "each existing issue comment is translated and written back as a labelled reply.",
            required=False,
            placeholder="off",
        ),
        FieldSpec(
            "max_comments",
            "Max comments per issue",
            "Safety cap when translating existing comments.",
            required=False,
            placeholder="20",
        ),
        FieldSpec(
            "max_results", "Max issues", "Safety cap per pull.", required=False, placeholder="50"
        ),
        FieldSpec(
            "auto_translate_on_change",
            "Auto-translate on push",
            "Off by default "
            "(button-trigger): a verified Automation/webhook push marks issues PENDING "
            "to release. On = translate on every verified push.",
            required=False,
            placeholder="off",
        ),
        FieldSpec(
            "attachment_translate",
            "Translate attachments",
            "Off by default. On = "
            "translate supported attachments and attach the result alongside the "
            "original, never overwriting.",
            required=False,
            placeholder="off",
        ),
        FieldSpec(
            "attachment_types",
            "Attachment file types",
            "Comma-separated extension "
            "allowlist (e.g. .docx, .pdf, .html). Blank = all supported formats; "
            "others skipped.",
            required=False,
            placeholder=".docx, .pdf, .html",
        ),
        # SPEC-context-metadata Phase 3B: the widened metadata fetch
        # (project/issuetype/labels) -> Document.context. On by default; set 0 to disable.
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "email",
            "Atlassian account email",
            "The account that owns the API token.",
            secret=False,
            placeholder="you@company.com",
        ),
        FieldSpec(
            "api_token",
            "API token",
            "Created at id.atlassian.com/manage-profile/security/api-tokens.",
            secret=True,
            placeholder="ATATT3xFfGF0…",
        ),
        FieldSpec(
            "webhook_secret",
            "Webhook / Automation secret",
            "Shared secret verifying "
            "inbound pushes; set the SAME value on the Automation rule / webhook. "
            "Blank = disable push intake.",
            secret=True,
            required=False,
            placeholder="a long random string",
        ),
    ],
    setup_steps=[
        "Go to id.atlassian.com → Security → Create and manage API tokens → Create API token.",
        "Choose plain 'Create API token', not 'Create API token with scopes': a scoped token "
        "returns 401 on your site URL. Copy the token and paste it here along with the email "
        "of that Atlassian account.",
        "Set the Site URL to your Jira Cloud base, e.g. https://your-team.atlassian.net.",
        "Write a JQL that selects the issues to localize (a dedicated status or label works well).",
    ],
    other_side="Optional: deploy the Localization OS Jira app (integrations/jira-forge-app) "
    "to add a “Localize with Localization OS” panel directly on the issue view.",
    docs_url="https://developer.atlassian.com/cloud/jira/platform/rest/v3/",
    supports_oauth=True,
)


JIRA_SUMMARY_MAX_CHARS = 255


JQL_GET_MAXLEN = 1500


_TRANSLATABLE_CF_TYPES = frozenset({"string"})


_RICHTEXT_CF_SUFFIXES = (":textarea",)


_ADF_CF_SCHEMA_TYPES = frozenset({"doc", "adf"})


_BUILTIN_TEXT_FIELDS = frozenset({"summary", "description"})


_ADF_FIELDS = frozenset({"description", "environment"})


# SPEC-context-metadata Phase 3B: the widened metadata fetch
# (project/issuetype/labels) -> Document.context. On by default; set 0 to disable.
# F3 (review): Jira's `summary` field is capped at 255 chars server-side (a PUT
# over that 400s). Some source languages expand a LOT in translation (German,
# Finnish, ...), so a summary that fits in the source can legitimately overflow in
# the target. See store.jira_translate's pre-check: we SKIP the summary write with
# a clear reason rather than truncate (a truncated issue title silently loses
# meaning — a skipped write is honest and converges once corrected) and rather
# than let Jira 400 the same pair every run forever (fails safe but never
# converges, per the review finding).
# G7: a scoped JQL clause longer than this switches _search/approximate-count from a
# GET query param to a POST body (Atlassian's documented remedy for a JQL "too large
# to be encoded as a query parameter"). Conservative — the whole GET URL (host + path
# + fields + percent-encoding) stays well under typical 4-8k server limits below this,
# and the clause is the only unbounded part. The scope guard is identical on either
# transport (both carry the same already-scoped clause; see _search / _scoped_clause).
# Plain single-line / read-only text custom fields (schema.type == "string" with a
# BARE-STRING value). Rich-text (paragraph) custom fields are handled separately —
# their value is an ADF doc, so they route through the ADF walker (see below).
# Custom-field schema.custom keys whose stored VALUE is ADF rich text (a Jira
# "Paragraph"/rich-text field, e.g. an "Acceptance Criteria" field). Matched by
# SUFFIX so a tenant/app prefix variant still resolves. Translated through the SAME
# app.filters.adf walker as `description`, so inline marks/mentions/links survive.
# Some app/Forge rich-text custom fields report an ADF-ish schema.type directly
# (rather than a :textarea schema.custom); accept those too. This is the G1 widening
# beyond the old exact `schema.type == "string"` gate that silently skipped every
# rich-text custom field whose type string was not literally "string".
# Built-in fields we always allow without the field catalogue (their types are fixed).
# Built-in fields whose value is ADF (rich text), not a bare string.
def _safe_attachment_path(path: str) -> str:
    """Normalize a Jira-supplied attachment path before it is appended to an API base
    (review LOW, defense in depth). The `content` URL is Jira-server-generated, so this
    is not a known-exploitable input, but `..`/`//` in the path must never survive to the
    wire: collapse `//` and reject any traversal so the resolved URL can only reach a
    canonical in-perimeter path. Returns the cleaned absolute path (always leading `/`,
    no `..` segment, no protocol-relative `//host` slip)."""
    p = "/" + (path or "").lstrip("/")  # force a single leading slash
    while "//" in p:
        p = p.replace("//", "/")
    segments = p.split("/")
    if ".." in segments or "." in segments:
        raise ConnectorError("attachment path contains a traversal segment ('..'/'.') — refused")
    return p


def _cf_schema_kind(schema) -> tuple[str, str]:
    """Classify a custom field by its /rest/api/3/field `schema` block — the
    SCHEMA-DRIVEN selection the coverage taxonomy (§3-D) requires. Returns
    ("adf"|"plain"|"", reason):

      * "adf"   — a rich-text/paragraph field (value is an ADF doc): routed through
                  the ADF walker exactly like `description` (marks/mentions/links
                  preserved). This is the G1 headline: previously skipped.
      * "plain" — a single-line/read-only text field (bare-string value).
      * ""      — a NON-text field (number/date/option/array/user/version/...):
                  SKIPPED with a typed reason, never written blind. The reason names
                  the actual schema type so the operator sees WHY it was skipped
                  (reported, not silently dropped)."""
    schema = schema if isinstance(schema, dict) else {}
    t = (schema.get("type") or "").strip().lower()
    custom = (schema.get("custom") or "").strip().lower()
    if any(custom.endswith(sfx) for sfx in _RICHTEXT_CF_SUFFIXES) or t in _ADF_CF_SCHEMA_TYPES:
        return "adf", ""
    if t in _TRANSLATABLE_CF_TYPES:
        return "plain", ""
    return "", f"custom field type '{t or 'unknown'}' is not translatable"


_LABEL_PREFIX = "🌐"


# F1(b) (review, self-feedback loop): every comment WE post is labelled with this
# prefix (see post_comment). fetch_comments must exclude our OWN replies BEFORE
# they can be picked up as translatable "existing comments" — without this,
# translate_comments would translate our own reply, post ANOTHER labelled reply
# translating THAT, and so on (proven exponential 1->3->7 in review).
#
# D6 (audit — spoofable-marker + data loss): the decisive check is AUTHORSHIP, not
# the marker. fetch_comments compares each comment's author.accountId against our
# OWN authenticated accountId (from /rest/api/3/myself, cached per connector
# instance in _self_account_id). This closes two defects the bare prefix check had:
#   (a) spoofable — any human/integration could post a 🌐-prefixed comment and have
#       it silently treated as ours (dropped from translation);
#   (b) data loss — a legitimate human comment that merely HAPPENS to start with
#       🌐 was silently dropped.
# The marker is kept only as a SECONDARY fallback for the degraded case where our
# own accountId cannot be fetched (e.g. /myself unreachable): there we fall back to
# the old prefix behaviour so the anti-repost loop still has some protection. In
# the normal path authorship is decisive, so a human 🌐 comment by a different
# account is never dropped and a spoofed 🌐 comment by another author still
# translates.
def _adf_text(node) -> str:
    """Plain text out of an ADF value — now the AST walker's `plain_text` (leaf
    text only; code content and opaque-node ids are never surfaced as prose).
    Kept as a name so read-side callers (title previews) don't churn."""
    return adf.plain_text(node)


def _adf_doc(text: str) -> dict:
    """Plain text → a valid ADF document (one paragraph per blank-line block).

    Retained for the PLAIN-STRING write paths (a labelled comment assembled from
    already-translated plain segments, or a plain-text summary). Rich ADF fields
    (description) are written by reinserting into the SOURCE tree via the walker,
    NOT by round-tripping through this flattener — see JiraConnector.set_field_adf."""
    paras = [
        " ".join(ln.strip() for ln in p.splitlines() if ln.strip())
        for p in re.split(r"\n\s*\n", (text or "").strip())
    ]
    content = [{"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paras if p]
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph"}]}


_META_FIELDS = ("project", "issuetype", "labels", "summary")


# Context-harvest metadata fields (SPEC-context-metadata Phase 3B). fetch_issues adds
# these to the API `fields=` request so Jira RETURNS them, but they are METADATA ONLY —
# extracted into `issue["meta"]`, never translated as units. summary is here too so it
# is available as source.title even when it is not a configured content field.
def _issue_meta(f: dict) -> dict:
    """Pluck the harvestable, NON-translatable metadata off a Jira issue's fields
    block: project name -> domain, issuetype name -> content_kind, labels. Defensive
    against missing/oddly-shaped expands (permission-trimmed results, older sites) —
    a missing field yields None/[] and the harvest mapper simply omits it."""
    proj = f.get("project") if isinstance(f.get("project"), dict) else {}
    itype = f.get("issuetype") if isinstance(f.get("issuetype"), dict) else {}
    labels = [str(x) for x in (f.get("labels") or []) if isinstance(x, str) and x.strip()]
    return {
        "project": (proj.get("name") or "").strip() or None,
        "issuetype": (itype.get("name") or "").strip() or None,
        "labels": labels,
    }


_WEBHOOK_SECRET_HEADER = "x-loc-webhook-secret"  # noqa: S105 -- a header name, not a secret  # pregate: allow-secret not a credential


# ============================================================================
# Jira Phase 2 — push intake (Automation "Send web request" + raw REST webhooks)
# ----------------------------------------------------------------------------
# Registered on the GENERALIZED intake (POST /webhooks/connectors/{id},
# webhooks.py). This module only turns a VERIFIED delivery into changed issue
# key(s); the posture decision (auto-translate vs record-pending) lives in the
# route's enqueue callback (store.jira_auto_translate_on), and the job re-fetches
# each issue via jira_translate -- thin payloads are never trusted for content.
#
# RESEARCH-GROUNDED AUTH:
#   * Automation "Send web request" (the maintainer's PRIMARY, no-app path) CANNOT HMAC-sign
#     a body, but CAN send a CUSTOM HEADER. So we accept a per-connection SHARED
#     SECRET in `X-Loc-Webhook-Secret`, constant-time compared (hmac.compare_digest)
#     against the vaulted `webhook_secret`. [CONFIRMED capability: custom headers.]
#   * Raw REST webhooks (admin path) sign with WebSub HMAC-SHA256 in
#     `X-Hub-Signature: sha256=<hmac>` over the UTF-8 body. [CONFIRMED, research S3.]
#     We verify that too, so a manually-created site webhook that DOES sign is
#     accepted.
#   * [UNCERTAIN / NOT BUILT] Dynamic registration via POST /rest/api/3/webhook is
#     restricted to Connect/OAuth apps AND requires the registered URL to share the
#     app's base URL (research S3, registration paths #2) -- it is NOT available to
#     an email+API-token basic-auth caller like ours. So we do NOT implement REST
#     register/extend/delete or a 30-day extend loop against unconfirmed mechanics.
#     The admin path is: create a SITE webhook once in Jira (a site admin uses
#     /rest/webhooks/1.0/webhook or the admin UI) pointing at our intake URL, with
#     either the signing secret (-> X-Hub-Signature) or, for the Automation path,
#     the shared secret header. Documented on the connection page.
#
# A SINGLE verifier accepts EITHER scheme so one connection serves both the
# Automation (header) and raw-webhook (signature) senders without reconfiguration.
def _jira_header_secret_ok(secret: str, body: bytes, headers: dict) -> bool:
    """Constant-time match of the per-connection shared secret carried in
    X-Loc-Webhook-Secret (the Automation path -- Automation cannot HMAC-sign)."""
    import hmac as _hmac

    provided = ""
    for k, v in (headers or {}).items():
        if k.lower() == _WEBHOOK_SECRET_HEADER:
            provided = (v or "").strip()
            break
    if not provided:
        return False
    return _hmac.compare_digest(provided, (secret or "").strip())


# WebSub HMAC-SHA256(body, secret), hex digest, `sha256=` prefix -- the raw REST
# webhook signature (the de-facto scheme; the research gives the WebSub test vector
# secret="It's a Secret to Everybody", payload="Hello World!").
def _jira_signature_ok(secret: str, body: bytes, headers: dict) -> bool:
    from app.services.connectors import webhooks as _wh

    return _wh.hmac_sha256_verifier("X-Hub-Signature", prefix="sha256=", hex_digest=True)(
        secret, body, headers
    )


def _jira_verifier(secret: str, body: bytes, headers: dict) -> bool:
    """Accept EITHER the shared-secret header (Automation) OR the WebSub signature
    (raw webhook). Both are constant-time; a delivery carrying neither is rejected."""
    return _jira_header_secret_ok(secret, body, headers) or _jira_signature_ok(
        secret, body, headers
    )


_ISSUE_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")


# A Jira issue key is PROJECTKEY-NUMBER: an alphanumeric project key (starting
# with a letter, Jira's own rule) then a dash then a bare integer -- NOTHING else.
# F1(a) (adversarial review, JQL injection / scope escape): the old gate only
# rejected '{' and ' ', so a value like "LOC-1)OR(project=SECRET" passed straight
# through and interpolated raw into fetch_issues' `key in (...)` clause (proven);
# "A-1,B-2" also passed and silently became TWO keys via comma-smuggling. A
# fullmatch allowlist closes both: anything that isn't exactly KEY-NUMBER is
# rejected outright (logged, not raised -- a malformed/hostile value in one shape
# must not abort parsing of the other shapes in the same payload).
def _jira_issue_keys(body_json) -> list[str]:
    """Issue key(s) a verified Jira push names. Parsed DEFENSIVELY across the shapes
    an Automation rule or a raw webhook can produce -- the Automation body is
    author-chosen (smart values), so we can't assume one shape:
      * {"issue_key": "LOC-1"} / {"issueKey": "LOC-1"} / {"key": "LOC-1"}
      * {"issue": {"key": "LOC-1"}}                       (raw webhook shape)
      * {"issue_keys": ["LOC-1","LOC-2"]} / {"keys": [...]}
      * {"issues": [{"key": "LOC-1"}, "LOC-2"]}
    THIN: we only take keys; the job re-fetches each issue's content. Every
    candidate must match the strict ABC-123 shape (_ISSUE_KEY_RE, F1(a) fix) --
    anything else (JQL-injection payloads, comma-smuggled multi-keys, smart-value
    braces) is rejected and logged, never passed through.

    D5 (audit): the list is CAPPED at the intake's _MAX_CHANGED_IDS DURING extraction
    -- membership dedup uses a set (O(1), not the old O(n^2) `not in out` list scan)
    and we STOP at the cap instead of parsing+regex-validating+logging every
    candidate of a hostile many-thousand-key body then truncating downstream."""
    from app.services.connectors.webhooks import _MAX_CHANGED_IDS  # lazy: import cycle

    body_json = body_json or {}
    out: list[str] = []
    seen: set[str] = set()
    capped = False

    def _add(v):
        nonlocal capped
        if len(out) >= _MAX_CHANGED_IDS:
            if not capped:
                log.warning(
                    "jira webhook: changed-issue-key list hit cap %d; remaining ids ignored",
                    _MAX_CHANGED_IDS,
                )
                capped = True
            return
        s = str(v or "").strip()
        if not s:
            return
        if s in seen:
            return
        if _ISSUE_KEY_RE.fullmatch(s):
            seen.add(s)
            out.append(s)
        else:
            log.warning(
                "jira webhook: rejected malformed issue key %r (payload dropped, not translated)",
                s[:120],
            )

    for k in ("issue_key", "issueKey", "key"):
        if isinstance(body_json.get(k), str):
            _add(body_json.get(k))
    iss = body_json.get("issue")
    if isinstance(iss, dict):
        _add(iss.get("key"))
    for listkey in ("issue_keys", "issueKeys", "keys"):
        for v in body_json.get(listkey) or []:
            _add(v)
            if capped:
                break
        if capped:
            break
    if not capped:
        for v in body_json.get("issues") or []:
            if isinstance(v, dict):
                _add(v.get("key"))
            else:
                _add(v)
            if capped:
                break
    return out


_NONCONTENT_CHANGELOG_FIELDS = frozenset(
    {
        "status",
        "resolution",
        "assignee",
        "reporter",
        "priority",
        "labels",
        "comment",
        "worklog",
        "watchers",
        "sprint",
        "rank",
        "flagged",
        "link",
        "issuetype",
        "fix version",
        "fix version/s",
        "fixversion",
        "version",
        "component/s",
        "components",
        "duedate",
        "timeestimate",
        "timeoriginalestimate",
        "timespent",
        "epic link",
        "epic child",
        "parent",
        "attachment",
        "issueparentassociation",
    }
)


# Changelog field-change gate: a Jira `changelog` (present in raw jira:issue_updated
# webhooks; an Automation rule can include it) lists per-field {field, fromString,
# toString}. A DENYLIST (not an allowlist) is used so the gate is robust for any
# translatable field including a configured custom field: the push re-translates
# UNLESS the changelog changed ONLY known non-content fields (status, workflow
# transitions, assignee, labels, priority, comment activity, ...). A changed field
# NOT on the denylist (summary, description, a text custom field) counts as content
# and translates. When the payload carries NO changelog we return True: the
# per-(issue,locale) content-hash gate in jira_translate is the authoritative
# backstop and suppresses an unchanged pair anyway, so a missing changelog is never
# a correctness hole (worst case a wasted, hash-suppressed run).
#
# NON-CONTENT changelog fields (lowercased `field`/`fieldId` values). Comment
# create/edit/delete surfaces as field "Comment"; status transitions as "status"
# (+ "resolution"); the rest are workflow/PM metadata we never translate.
def changelog_indicates_content_change(body_json) -> bool:
    """True when a push should re-translate:
    * no changelog in the payload -> True (hash gate backstops).
    * changelog present, at least one changed field is NOT a known non-content
      field (summary/description/a text custom field) -> True.
    * changelog present, EVERY changed field is non-content (status/comment/...)
      -> False (a status-only or comment-only touch never re-translates)."""
    body_json = body_json or {}
    cl = body_json.get("changelog")
    items = None
    if isinstance(cl, dict):
        items = cl.get("items")
    elif isinstance(cl, list):
        items = cl
    if not items:
        return True  # no changelog -> defer to the hash gate (translate)
    for it in items:
        if not isinstance(it, dict):
            continue
        # Jira changelog items name the field as `field` (built-ins: "summary",
        # "description", "status") or `fieldId` (custom fields: "customfield_10050").
        fld = str(it.get("field") or it.get("fieldId") or "").strip().lower()
        if not fld:
            continue
        if fld not in _NONCONTENT_CHANGELOG_FIELDS:
            return True  # a content (or custom) field changed -> translate
    return False  # only non-content fields changed -> skip


# ===========================================================================
# Jira outbound-attachment translation — relocated from store.py (Wave 3 store.py
# decomposition). Jira-only; store.py keeps thin delegators (_jira_translate_
# attachments is called by store.jira_translate + the verify suites; _attachment_
# allowlist by the verify suites). The SSRF caps (MAX_ATTACHMENT_BYTES), the
# extension allowlist, egress guard, WriteBudget ceiling, content-hash suppression,
# and the self-authored/own-name loop-guard are carried across UNCHANGED.
# ===========================================================================
# --- Jira outbound attachments (SPEC-attachments Phase A, tunnel-free) -----------
# Translate the TEXT INSIDE files attached to an issue (docx/pdf/html/…) through the
# EXISTING file-filter + document pipeline (ingest -> pretranslate -> export) and
# attach the rebuilt translated file back ALONGSIDE the original (additive). Every
# jira-flow guard is honored: egress guard, WriteBudget ceiling (each upload is a
# write), content-hash suppression keyed by (source-attachment-id, locale), refusal-
# safe rebuild (see _build_attachment_bytes), org isolation (docs land in the
# connection's org-scoped project via _ensure_project), and the loop-guard (an
# attachment WE authored, or named like our own output, is never re-translated). The
# Airtable/inbound half stays tunnel/Fly-gated (Phase B), deliberately out of scope.
#
# RETENTION / ERASURE (accurate, not "erasable for free"): each pulled attachment is
# cached as a Document (encrypted at rest) inside the CONNECTION'S org-scoped Project.
# store.delete(cid) removes the Connection + its sync/webhook child rows ONLY — it does
# NOT delete that Project or its cached attachment Documents (pre-existing platform
# behavior for ALL connectors; auto-deleting the project would also destroy human
# post-edits + TM, a policy call reserved for the operator). Full erasure of the pulled
# content = delete the Project (the existing project-delete user action). The delete
# seams (web flash + CLI output) say so explicitly.
def _attachment_allowlist(cfg) -> set:
    """The set of file extensions (no leading dot, lowercased) attachment
    translation will accept: the connection's `attachment_types` list INTERSECTED
    with the filter registry's supported_exts (so a typo / unsupported ext can never
    slip through), or every supported ext when the list is blank.

    EXCEPTION — image types are excluded from the blank/"all supported" default.
    ImageFilter (SPEC-image-localization) registers png/jpg/jpeg/svg into the filter
    registry the moment the module is imported, independent of the module kill switch.
    Connector routing of images is Phase 3 (SPEC sec 3.4 / 6), NOT Phase 1: leaving
    them in the blank default would silently pull image attachments into a LIVE run
    (per-image VLM spend + re-rendered images pushed to the tenant) for any connection
    with attachment translation on and no explicit type list — a behavior change coupled
    to nothing the operator opted into. Image exts therefore require an EXPLICIT
    allowlist entry (e.g. attachment_types = ".png") to be translated; that explicit
    opt-in IS the Phase 3 enablement mechanism."""
    from app.filters import supported_exts
    from app.services.image_localization import IMAGE_EXTS

    supported = set(supported_exts())
    raw = (cfg.get("attachment_types") or "").strip()
    if not raw:
        return supported - set(IMAGE_EXTS)
    allow = {e.strip().lower().lstrip(".") for e in raw.replace(";", ",").split(",") if e.strip()}
    return allow & supported


def _translated_attachment_name(filename: str, loc: str) -> str:
    """{stem}.{locale}.{ext} (SPEC step 5). Also the shape the loop-guard's
    filename fallback recognizes as ours."""
    if "." in filename:
        stem, ext = filename.rsplit(".", 1)
        return f"{stem}.{loc}.{ext}"
    return f"{filename}.{loc}"


def _is_our_attachment_name(filename: str, locales) -> bool:
    """Belt-and-suspenders loop-guard, consulted ONLY in DEGRADED mode (our /myself
    accountId is unavailable, so the primary authorship check could not run): a
    filename shaped like our OWN output {stem}.{loc}.{ext} for one of THIS run's target
    locales is treated as ours and skipped, so a translated file is never re-pulled as
    a source attachment. When authorship IS resolvable it is authoritative and this
    fallback is NOT applied — a human file that merely looks like our output is still
    translated (caller gates this on `not self_aid`)."""
    parts = (filename or "").rsplit(".", 2)
    return len(parts) == 3 and parts[1] in set(locales or [])


def _attachment_bytes_hash(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data or b"").hexdigest()[:32]


def _build_attachment_bytes(doc_id: int, locale: str) -> bytes:
    """Rebuild the translated attachment file for `locale`, reusing the standard
    document export (storage.read_upload -> filter.build with the paragraph map).

    REFUSAL-SAFETY IS STRUCTURAL, not a post-hoc scrub: a suspected refusal never
    reaches target_text at all — the choke point (quality.commit_mt_output) routes
    a flagged segment to NEEDS_REVIEW with target_text left EMPTY. export then falls
    back `target_text or source_text` -> the SOURCE text for that segment, so an
    assistant hedge can NEVER land in a customer-facing attachment. Untranslated
    segments likewise fall back to source, keeping the whole file COHERENT (a
    document is one artifact — unlike jira_translate's discrete field/comment writes,
    silently dropping segments here would corrupt it). This is deliberately the SAME
    untranslated->source convention export.py already uses for every document, so
    connector attachments and manual document exports behave identically.

    BATCH 15 T2 EXCEPTION (A-LOW-5/B-LOW-8, maintainer-ratification pending): the untranslated->
    source fallback above is now SUPPRESSED for a (doc, locale) that carries a COVERING
    empty-delivery approval -- `build_translated_file` self-resolves the deliver-empties emit
    mode, so an APPROVED tm_match deliverable's Jira attachment ships EMPTY strings (what the
    human approved), NOT source. An UNAPPROVED held target is skipped by `_locale_is_held`
    exactly as before (no source egress), so the ONLY changed shape is the deliberately-
    approved one. This narrows spec N6's "Jira UNCHANGED" claim to the unapproved path; the
    approved path now follows the deliver-empties semantics consistently. Connector-push
    empty gating in general stays deferred (§19-Q4)."""
    from app.services import export

    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        return export.build_translated_file(s, doc, locale)


def _locale_is_held(doc_id: int, locale: str) -> bool:
    """True when (doc, locale) has NO committed translation, so export would fall back to
    pure SOURCE bytes -- the mock-egress failure shape (an upload named {stem}.{loc}.{ext}
    that LOOKS translated but isn't). Folded onto the ONE held-predicate seam
    (export.held_locale_reason, review M3) so the connector and every document-export
    surface answer "is there anything to deliver?" identically. The refusal choke point
    (quality.commit_mt_output) leaves a flagged segment's target_text EMPTY, so an
    all-refused document is held.

    NO-SEGMENTS => HELD (review guard): held_locale_reason treats "no segments for this
    locale" as NOT held (None) -- correct for a document surface (its route 404s an absent
    locale). But this connector runs the check to decide whether to UPLOAD an attachment,
    and a locale with no segments would export pure SOURCE just the same. So a document
    with no segments for `locale` at all is held HERE -- an explicit guard so a future
    reorder (e.g. locale-add running before ingest, so segments don't exist yet at check
    time) can never silently re-enable source egress."""
    from app.services import export

    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        has_seg = (
            s.exec(
                select(Segment.id)
                .where(Segment.document_id == doc_id, Segment.target_locale == locale)
                .limit(1)
            ).first()
            is not None
        )
        if not has_seg:
            return True  # nothing to deliver -> held (never upload a source-only file)
        # ISSUE #95 (gate C round 1, H1): a QUALITY-GATE hold is a delivery hold too. An
        # attachment upload IS a delivery, and text a judge FAILED with no repair is worse
        # to egress than no translation at all -- so this seam consults both predicates.
        # (The TM-only EMPTY gate stays out of the connector by design, v1 note N6; that
        # one is about MISSING text, and its way out is an audited approval this path has
        # no surface for. This one only ever refuses MORE.)
        if export.gate_held_reason(s, doc, locale) is not None:
            return True
        return export.held_locale_reason(s, doc, locale) is not None


def _jira_translate_attachments(
    cid: int,
    issue_keys,
    *,
    locales=None,
    target_locales=None,
    actor: str = "",
    transport=None,
    budget=None,
    jr=None,
) -> dict:
    """Translate + re-attach every eligible attachment on `issue_keys`. Returns a
    structured attachment summary (merged into jira_translate's result). Callable
    standalone (own WriteBudget + connector) or from jira_translate (shared budget +
    connector). See the section header for the guard inventory."""
    from app.filters import get_filter
    from app.services import ingest, translate
    from app.services.connectors.store import (
        WriteBudget,
        _add_missing_locales,
        _ensure_project,
        build_fresh,
        effective_locales,
        get,
        patch_config_merge,
    )

    result = {
        "attachments_translated": 0,
        "attachments_written": 0,
        "attachments_skipped": 0,
        "attachments_suppressed": 0,
        # attachments_untranslated: a doc whose segments ALL refused/QE-flagged
        # for a locale (export would emit pure SOURCE bytes under a
        # {stem}.{loc}.{ext} name — a fake translation), so the upload is skipped.
        "attachments_untranslated": 0,
        # attachments_loopguard_skipped: files dropped by the DEGRADED-mode
        # filename loop-guard (only fires when /myself is unresolvable).
        "attachments_loopguard_skipped": 0,
        "attachment_errors": [],
        "attachment_failures": [],
    }
    conn = get(cid)
    if not conn or conn.kind != "jira" or not issue_keys:
        return result
    cfg = conn.config or {}
    if locales is None:
        locales = effective_locales(conn, target_locales)

    # Egress guard (SPEC guardrail): refuse mock-MT -> real-Jira attachment upload up
    # front. No-op under the hermetic suite (MockTransport) and with a live provider.
    try:
        egress_guard.assert_egress_allowed(conn, transport, locales)
    except egress_guard.MockEgressBlocked as e:
        result["attachment_errors"].append(str(e))
        return result

    if jr is None:
        jr = build_fresh(cid, transport)
    if budget is None:
        budget = WriteBudget()

    # Snapshot conn attributes we need INSIDE the loop before _ensure_project's first
    # commit expires this detached instance (else a later conn.<attr> read would raise
    # DetachedInstanceError on the 2nd+ attachment).
    created_by = actor or (conn.created_by or "")
    allow = _attachment_allowlist(cfg)
    max_bytes = config.MAX_ATTACHMENT_BYTES

    # Loop-guard identity (best-effort; "" -> the filename-pattern fallback only).
    self_aid = ""
    try:
        with jr._client() as c:
            self_aid = jr._self_account_id(c)
    except Exception:
        self_aid = ""

    hashes = dict(cfg.get("jira_attachment_hashes") or {})
    hash_delta: dict = {}
    proj_id = None

    for issue_key in issue_keys:
        if budget.ceiling_hit:
            break
        try:
            atts = jr.list_attachments(issue_key)
        except Exception as e:
            result["attachment_errors"].append(
                f"{issue_key}: list attachments failed: {type(e).__name__}: {e}"
            )
            continue
        for att in atts:
            if budget.ceiling_hit:
                break
            fn = att["filename"]
            att_id = att["id"]
            # Loop-guard (primary: authorship; fallback: our filename shape).
            if self_aid and att.get("author_id") and att["author_id"] == self_aid:
                continue
            # The filename fallback is a DEGRADED-MODE guard ONLY: consult it just when
            # our /myself accountId is unresolvable (self_aid == ""), so the authorship
            # check above could not run. With a HEALTHY self_aid that check is
            # authoritative — a human-uploaded file that merely LOOKS like our output
            # ({stem}.{loc}.{ext}) is still real source work and MUST be translated, not
            # silently dropped (the prior unconditional filter was a data-loss bug). When
            # the degraded fallback DOES fire we count + log it so the skip is never silent.
            if not self_aid and _is_our_attachment_name(fn, locales):
                result["attachments_loopguard_skipped"] += 1
                log.info(
                    "jira_translate_attachments: connection %s skipped '%s' via the "
                    "DEGRADED filename loop-guard (/myself unavailable - authorship "
                    "unverifiable)",
                    cid,
                    fn,
                )
                continue
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            if ext not in allow or get_filter(ext) is None:
                result["attachments_skipped"] += 1
                result["attachment_errors"].append(
                    f"{issue_key}/{fn}: type '{ext or 'none'}' unsupported / not allowlisted — skipped"  # noqa: E501
                )
                continue
            # Size guard BEFORE download (metadata), then again on the bytes.
            if att["size"] and att["size"] > max_bytes:
                result["attachments_skipped"] += 1
                result["attachment_errors"].append(
                    f"{issue_key}/{fn}: {att['size']} bytes over cap {max_bytes} — skipped"
                )
                continue
            # Cluster A (adversarial audit): the WriteBudget is a SPEND ceiling, not just
            # a write ceiling — download + ingest + pretranslate are the BILLABLE steps.
            # If no writes remain this run, stop BEFORE the download so we never pay to
            # translate an attachment we can't upload (this compounds across locales).
            # Latch ceiling_hit so the loud per-run message fires; the remainder is
            # picked up next run (nothing is hashed until it actually uploads).
            if budget.remaining() == 0:
                budget.ceiling_hit = True
                break
            try:
                data = jr.download_attachment(att["content"], max_bytes=max_bytes)
            except Exception as e:
                result["attachment_errors"].append(
                    f"{issue_key}/{fn}: download failed: {type(e).__name__}: {e}"
                )
                continue
            if data is None:
                result["attachments_skipped"] += 1
                result["attachment_errors"].append(
                    f"{issue_key}/{fn}: download empty / over cap — skipped"
                )
                continue

            # Content-hash suppression (SPEC step 5): which locales still need it?
            chash = _attachment_bytes_hash(data)
            todo = []
            for loc in locales:
                if hashes.get(f"{att_id}:{loc}") == chash:
                    result["attachments_suppressed"] += 1
                else:
                    todo.append(loc)
            if not todo:
                continue

            # Cluster A: trim this attachment's still-needed locales to the remaining
            # write budget BEFORE the (billable) ingest + pretranslate loop — each
            # surviving locale is exactly one upload. A trimmed locale is not translated,
            # not uploaded, and not hashed, so it stays pending for the next run; trim()
            # records the allowed writes and latches ceiling_hit iff it clamped. This is
            # the sole budget accounting for the upload loop below (no per-upload
            # allow()/record()), mirroring the Confluence child-page flow.
            todo = todo[: budget.trim(len(todo))]
            if not todo:
                break

            # Ingest ONCE (parse -> Document + Segments + encrypted-at-rest upload),
            # then MT + rebuild + upload per still-needed locale.
            try:
                with Session(engine) as s:
                    if proj_id is None:
                        proj_id = _ensure_project(s, conn).id
                    proj = s.get(Project, proj_id)
                    _add_missing_locales(s, proj_id, todo)
                    s.commit()
                    doc = ingest.ingest_document(
                        s, proj, fn, data, TransMode.SEGMENTED, created_by=created_by
                    )
                    doc_id = doc.id
                    n_units = len((doc.skeleton or {}).get("units") or [])
            except ingest.UnsupportedFormat:
                result["attachments_skipped"] += 1
                result["attachment_errors"].append(
                    f"{issue_key}/{fn}: unsupported format — skipped"
                )
                continue
            except Exception as e:
                result["attachment_errors"].append(
                    f"{issue_key}/{fn}: ingest failed: {type(e).__name__}: {e}"
                )
                continue
            if n_units == 0:
                result["attachments_skipped"] += 1
                result["attachment_errors"].append(
                    f"{issue_key}/{fn}: no translatable text — skipped"
                )
                continue

            try:
                with Session(engine) as s:
                    for loc in todo:
                        translate.pretranslate_document(s, doc_id, loc)
            except providers.DeadEngineError as e:
                # Dead engine ref -> never upload mock/placeholder output.
                result["attachment_errors"].append(f"{issue_key}/{fn}: {e}")
                continue

            # Budget was already spent by trim() above, so no per-upload allow()/record().
            wrote_any = False
            for loc in todo:
                # FINDING 1 (mock-egress shape): if EVERY segment for this locale
                # refused / QE-flagged, nothing committed -> export would fall back to
                # pure SOURCE bytes and we'd upload {stem}.{loc}.{ext} that only LOOKS
                # translated. SKIP the upload, count it, record a RETRYABLE failure (a
                # re-run after human review re-attempts it), and DO NOT hash the pair.
                # A PARTIALLY-refused doc keeps the coherent per-segment source fallback.
                if _locale_is_held(doc_id, loc):
                    result["attachments_untranslated"] += 1
                    result["attachment_failures"].append(
                        {
                            "issue_key": issue_key,
                            "filename": fn,
                            "locale": loc,
                            "error": "all segments refused/flagged - not uploaded",
                        }
                    )
                    log.warning(
                        "jira_translate_attachments: connection %s '%s' [%s]: all "
                        "segments refused/flagged - NOT uploaded (would egress source "
                        "as a fake translation); left pending for re-run after review",
                        cid,
                        fn,
                        loc,
                    )
                    continue
                try:
                    out_bytes = _build_attachment_bytes(doc_id, loc)
                except Exception as e:
                    result["attachment_failures"].append(
                        {
                            "issue_key": issue_key,
                            "filename": fn,
                            "locale": loc,
                            "error": f"build: {type(e).__name__}: {e}",
                        }
                    )
                    continue
                out_name = _translated_attachment_name(fn, loc)
                ok_up, m_up, _new_id = jr.upload_attachment(
                    issue_key, out_name, out_bytes, att["mimeType"]
                )
                if ok_up:
                    result["attachments_written"] += 1
                    wrote_any = True
                    hashes[f"{att_id}:{loc}"] = chash
                    hash_delta[f"{att_id}:{loc}"] = chash
                else:
                    result["attachment_failures"].append(
                        {"issue_key": issue_key, "filename": fn, "locale": loc, "error": m_up}
                    )
            if wrote_any:
                result["attachments_translated"] += 1

    if hash_delta:
        # Merge-persist (concurrency-safe) so an already-translated (attachment,
        # locale) is suppressed next run — the loop-guard's second layer.
        patch_config_merge(cid, "jira_attachment_hashes", hash_delta)
    result["attachments_ceiling_hit"] = budget.ceiling_hit
    if budget.ceiling_hit:
        log.warning(
            "jira_translate_attachments: connection %s hit MAX_WRITES_PER_RUN=%s",
            cid,
            budget.ceiling,
        )
    return result
