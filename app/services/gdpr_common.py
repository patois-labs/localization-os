"""gdpr: shared leaf helpers (layered pure move)

Documented in: docs/architecture/data-and-migrations.md
"""

import logging
import re
from datetime import datetime

from sqlalchemy import Text, func, update
from sqlalchemy import delete as sa_delete
from sqlmodel import Session, select

from app.db import engine
from app.models import (
    AdvancedMission,
    AdvancedRun,
    Connection,
    ContentType,
    ContextProfile,
    Customer,
    CustomFieldDef,
    DatabricksExportRun,
    DataExportRun,
    DeliveryEmptyApproval,
    DepartmentBudget,
    Document,
    EngineProfile,
    EvalCandidate,
    EvalDataset,
    EvalDatasetItem,
    EvalRun,
    Glossary,
    GlossaryConcept,
    Invite,
    Job,
    JobQueue,
    KeyChangeLog,
    KeyImage,
    LanguageSet,
    LQAResult,
    MemoryEntry,
    MemoryEvent,
    MemorySuggestion,
    NotifyChannel,
    NotifyOutbox,
    NotifyRoute,
    Project,
    ProjectAutomation,
    ProjectBudget,
    ProjectFieldValue,
    ProjectGlossary,
    ProjectMember,
    ProjectTemplate,
    ProjectTmSource,
    QualityGoldItem,
    QualityGoldLabel,
    QualityLens,
    QualityLensExample,
    QueryPost,
    QueryThread,
    Role,
    RoutingRule,
    SegmentationProfile,
    SegmentPostEdit,
    StyleGuide,
    SyncLog,
    TapAction,
    TermCandidate,
    User,
    UserGroupMember,
    Vendor,
    WorkflowBatch,
    WorkflowGroup,
    WorkflowRun,
    WorkflowStep,
)
from app.services import access, apikeys, audit

_log = logging.getLogger("loc_tms.gdpr")


_BATCH = 500


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


_EMAIL_CHAR = r"[A-Za-z0-9._%+\-]"


# rows per DB round-trip while streaming an export
# Standard-ish email pattern, used to (a) find email inside JSON blobs and
# (b) mask OTHER people's emails out of the subject's exported audit rows.
# Character class that may appear inside an email's local-part or domain. The ONE
# uniform anchoring rule every SUBSTRING path shares (round-2 fix): match the subject's
# address only as a WHOLE address, never as a substring of a LONGER one.
def _anchored(email: str):
    """Compile a regex matching `email` ONLY as a whole address — never inside a longer
    one. Lookbehind/lookahead forbid an adjacent local-part/domain character, so
    `ann@corp.com` matches neither `joann@corp.com` (prefix) nor `ann@corp.com.mx`
    (suffix) nor `annx@corp.com`. `re.escape` neutralizes the address's own `.`/`+`.
    Shared by EVERY substring path (KeyChangeLog.actor + Job JSON rewrites, and the
    LIKE-confirm export/scan walks) so they all carry ONE anchored-substring semantics.

    Gate fix-gdpr round 1: the boundaries are Unicode-aware (`\\w`, so `jösé` or `müller`
    local parts are not split), an apostrophe INSIDE a local part (o'brien) counts as part
    of the address while a quote before it ('ann@x.test') does not, a sentence-final period
    ends the address (`pete@x.test.` matches, `ann@corp.com.mx` still does not), and the
    match is case-insensitive, the way accounts are matched everywhere else (login and the
    audit filter compare lowercased addresses)."""
    before, after = r"(?<![\w.%+\-])(?<!\w')", r"(?![\w%+\-]|\.\w)"
    return re.compile(before + re.escape(email) + after, re.IGNORECASE)


def _like_cond(columns, value):
    """The cheap SQL prefilter for the anchored confirm: any of `columns`, cast to text and
    lowercased, contains `value` (lowercased) OR its JSON-escaped form (a JSON column stores
    `müller@x.test` as `m\\u00fcller@x.test`, which a plain LIKE never hits). LIKE wildcards
    in the address are escaped, so `_` and `%` match literally. Always confirmed in Python
    with _anchored, so the prefilter only has to be a superset."""
    import json

    forms = {value.lower(), json.dumps(value)[1:-1].lower()}
    cond = None
    for col in columns:
        for form in sorted(forms):
            esc = form.replace("/", "//").replace("%", "/%").replace("_", "/_")
            term = func.lower(func.cast(col, Text)).like(f"%{esc}%", escape="/")
            cond = term if cond is None else cond | term
    return cond


def _mask_unless_subject(value: str, email: str) -> str:
    """Regex-free mask for a column KNOWN to hold a single email address: keep it iff it
    IS the subject's own (case-insensitively), else replace the WHOLE value with the
    placeholder. Used for the Glossary/StyleGuide two-column non-subject attribution
    (R2 MED-B): the field is an email-shaped attribution column, so masking must not
    depend on _EMAIL_RE matching its shape — an unconditional value compare closes the
    no-TLD / bare-word gap. Empty ("" = draft/unset) passes through unchanged."""
    if not value:
        return value
    return value if value.lower() == (email or "").lower() else "[redacted-email]"


def _tombstone(uid: int) -> str:
    return f"deleted-user-{uid}@erased.invalid"


def _org_scoped(cond, org_col, org_id, where=None):
    """AND an org-confinement predicate onto `cond` when a caller org is in force
    (multi-org P8). `org_id is None` (operator / single-tenant) leaves `cond`
    untouched, byte-identical to the unscoped path; a scoped caller narrows every
    org-stamped export section to their own org so a cross-org CONFIG row a subject's
    email happens to author never spills into an org-confined export (MED-2). Only
    used on models that carry an `org_id` column — the org-less ones (project/glossary-
    scoped rows) have no cheap org key and pass `org_col=None`; the export narrows those
    with `where` instead, a predicate through their project, document, connection or
    glossary (gate multiorg-inv round 1, LOW-2). `where=None` adds nothing."""
    if where is not None:
        cond = cond & where
    if org_id is None or org_col is None:
        return cond
    return cond & (org_col == org_id)


def _iter_by(model, column, value, shape, cursor0=0, org_col=None, org_id=None, where=None):
    """Yield `shape(row)` for every `model` row where `column == value`, in
    bounded id batches so a large table never materializes at once.

    `cursor0` seeds the keyset walk: 0 for the usual integer PK, "" for a model
    whose PK is a string (TapAction.id is a UUID string — an integer 0 seed would
    make `id > 0` a varchar-vs-integer type error on Postgres). Empty string is a
    valid low sentinel: every non-empty id sorts after it, lexicographically.

    `org_col`/`org_id` optionally org-confine the stream (MED-2, see _org_scoped), and
    `where` ANDs one more predicate (the export's org scope for org-less models)."""
    cursor = cursor0
    while True:
        with Session(engine) as s:
            cond = _org_scoped(column == value, org_col, org_id, where)
            rows = s.exec(
                select(model).where(cond, model.id > cursor).order_by(model.id).limit(_BATCH)
            ).all()
        if not rows:
            return
        for r in rows:
            cursor = r.id
            yield shape(r)


def _iter_by_any(model, columns, value, shape, org_col=None, org_id=None, where=None):
    """Like _iter_by but matches ANY of several columns (OR), deduplicated by id.
    `org_col`/`org_id` optionally org-confine the stream (MED-2); `where` as _iter_by."""
    cursor = 0
    while True:
        with Session(engine) as s:
            cond = columns[0] == value
            for c in columns[1:]:
                cond = cond | (c == value)
            cond = _org_scoped(cond, org_col, org_id, where)
            rows = s.exec(
                select(model).where(cond, model.id > cursor).order_by(model.id).limit(_BATCH)
            ).all()
        if not rows:
            return
        for r in rows:
            cursor = r.id
            yield shape(r)


def _iter_by_like(model, column, value, shape, org_col=None, org_id=None, where=None):
    """Like _iter_by but matches `value` as a SUBSTRING of a text column (LIKE
    %value% prefilter, confirmed with the ANCHORED regex in Python). Needed where the
    subject's email is EMBEDDED in a larger attribution string rather than being the
    whole value — KeyChangeLog.actor's 4th grammar ("workflow:wf<id>:<locale> by
    <email>", models.py; keys.py / web/keys.py). An exact `== value` match is
    structurally blind to that grammar (HIGH-1); a bare `value in actor` confirm
    over-matches a LONGER address (R2 MED-A: subject@example.com would export
    notsubject@example.com's rows) — so the confirm is anchored. Integer-PK models."""
    pat = _anchored(value)
    cursor = 0
    while True:
        with Session(engine) as s:
            cond = _org_scoped(_like_cond([column], value), org_col, org_id, where)
            rows = s.exec(
                select(model).where(cond, model.id > cursor).order_by(model.id).limit(_BATCH)
            ).all()
        if not rows:
            return
        for r in rows:
            cursor = r.id
            if pat.search(getattr(r, column.key) or ""):
                yield shape(r)


def _settings_subject_keys(settings, pat, email) -> dict:
    """Project customer settings or dataset metadata to subject occurrence paths.

    Keys and values are untrusted content: retain only paths to anchored occurrences,
    anonymize non-subject keys, and disclose no surrounding values (#380).
    A non-dict settings yields {}.
    """
    if not isinstance(settings, dict):
        return {}
    return _mask_settings_subtree(settings, email, pat)


def _mask_settings_subtree(blob, email, pat):
    """Copy only paths to the subject, dropping unrelated dict entries/list elements.

    Only an exact subject address survives as a key; other retained keys receive
    unique placeholders. String leaves use the subject mask, while numeric, boolean
    and null leaves are redacted even underneath a subject-address key.
    """
    if isinstance(blob, str):
        return _mask_unless_subject(blob, email)
    if isinstance(blob, dict):
        out = {}
        for k, v in blob.items():
            if not ((isinstance(k, str) and pat.search(k)) or _json_has_anchored(v, pat)):
                continue
            key = k if isinstance(k, str) and k.lower() == email.lower() else "[redacted-key]"
            if key in out:
                index = 2
                while f"{key}#{index}" in out:
                    index += 1
                key = f"{key}#{index}"
            out[key] = _mask_settings_subtree(v, email, pat)
        return out
    if isinstance(blob, list):
        return [_mask_settings_subtree(v, email, pat) for v in blob if _json_has_anchored(v, pat)]
    return "[redacted]"


def _json_has_anchored(blob, pat) -> bool:
    """True iff any string leaf OR dict key of a JSON-ish structure anchored-matches (via
    `pat`). Keys count: a payload can key a map by address (gate fix-gdpr round 1)."""
    if isinstance(blob, str):
        return bool(pat.search(blob))
    if isinstance(blob, dict):
        return any(
            (isinstance(k, str) and pat.search(k)) or _json_has_anchored(v, pat)
            for k, v in blob.items()
        )
    if isinstance(blob, list):
        return any(_json_has_anchored(v, pat) for v in blob)
    return False


def _iter_customers_by_settings(value, shape, org_id=None):
    """Yield `shape(customer, matched_keys)` for every Customer whose free-form
    `settings` JSON anchored-matches `value` among its string values (LIKE %value%
    prefilter on the serialized column, ANCHORED-confirmed in Python — R2 MED-C: a bare
    substring confirm over-matches a longer address). `matched_keys` is the narrowed
    subset (see _settings_subject_keys) so the export emits ONLY the keys naming the
    subject, never the whole blob. Customer contact emails (primary_contact/
    technical_contact + arbitrary /api-merged keys) live here, so an erased user listed
    as a contact is the subject's own occurrence and must surface in the Art.15 export
    (HIGH-3). Org-confined when a caller org is set."""
    pat = _anchored(value)
    cursor = 0
    while True:
        with Session(engine) as s:
            cond = _org_scoped(_like_cond([Customer.settings], value), Customer.org_id, org_id)
            rows = s.exec(
                select(Customer)
                .where(cond, Customer.id > cursor)
                .order_by(Customer.id)
                .limit(_BATCH)
            ).all()
        if not rows:
            return
        for r in rows:
            cursor = r.id
            matched = _settings_subject_keys(r.settings, pat, value)
            if matched:
                yield shape(r, matched)


def _iter_by_json(model, value, shape):
    """Yield `shape(row)` for every `model` row whose JSON payload/result columns
    name `value` as a WHOLE address in some string leaf or key. SQLite stores JSON columns
    as text, so a cheap LIKE prefilter narrows the scan; the match is then confirmed in
    Python with the ANCHORED regex (_anchored + _json_has_anchored), the same rule the
    erase-side Job scrub (_scrub_json_jobs) and the KeyChangeLog walk (_iter_by_like)
    use. A bare `value in text` confirm over-matched a LONGER address (#223: ann@x.test's
    export carried every job that named joann@x.test, payload and result whole)."""
    yield from _iter_by_json_columns(model, (model.payload, model.result), value, shape)


def _iter_by_json_columns(model, columns, value, shape, where=None):
    """_iter_by_json over any JSON `columns` of `model` (integer PK), with an optional
    extra `where` predicate (the export's org scope)."""
    pat = _anchored(value)
    cursor = 0
    while True:
        with Session(engine) as s:
            cond = _like_cond(columns, value)
            if where is not None:
                cond = cond & where
            rows = s.exec(
                select(model).where(model.id > cursor, cond).order_by(model.id).limit(_BATCH)
            ).all()
        if not rows:
            return
        for r in rows:
            cursor = r.id
            if any(_json_has_anchored(getattr(r, c.key), pat) for c in columns):
                yield shape(r)


def _json_text(blob) -> str:
    """Deterministic text rendering of a JSON-ish column for substring scans."""
    if blob is None:
        return ""
    import json

    try:
        return json.dumps(blob, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(blob)


def _mask_other_emails(text: str, keep: str) -> str:
    """Replace every email-like substring in `text` with a placeholder EXCEPT
    `keep` (the subject's own address, case-insensitively). Keeps the subject's
    audit rows a genuine subject-access copy without leaking third parties' PII."""
    if not text:
        return text
    keep_l = (keep or "").lower()

    def _sub(m):
        return m.group(0) if m.group(0).lower() == keep_l else "[redacted-email]"

    return _EMAIL_RE.sub(_sub, text)


# Any run of characters around an `@` up to whitespace, a quote, an angle bracket, a
# parenthesis, a comma or a semicolon: the grammar-free notion of "an address-like token".
# Deliberately NOT address-shaped (gate fix-gdpr round 1, M1): no-dot, IP-literal, IDN,
# non-ASCII and numeric-TLD addresses are all masked, which _EMAIL_RE would miss.
_AT_TOKEN = re.compile(r"[^\s\"'<>(),;]*@[^\s\"'<>(),;]*")
_REDACTED = "[redacted-email]"


def _mask_text_but_subject(text: str, pat) -> str:
    """Mask every `@` token in `text` except the subject's own anchored matches. The
    subject's spans are located FIRST and copied through verbatim, so an o'brien-style
    address the token rule would split is never mangled; everything between them is
    masked token by token."""
    if not isinstance(text, str) or "@" not in text:
        return text
    out, pos = [], 0
    for m in pat.finditer(text):
        out.append(_AT_TOKEN.sub(_REDACTED, text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_AT_TOKEN.sub(_REDACTED, text[pos:]))
    return "".join(out)


def _mask_json_other_emails(blob, keep: str, _pat=None):
    """Return a COPY of a JSON-ish structure in which every `@` token that is not the
    subject's own address (`keep`, anchored, case-insensitive) is replaced by
    `[redacted-email]`, in string leaves AND dict keys. Kept for compatibility, not used
    by export_user since #380. Two keys that mask to the same text
    stay distinct (`[redacted-email]#2`, ...), so no value is silently dropped."""
    pat = _pat if _pat is not None else _anchored(keep)
    if isinstance(blob, str):
        return _mask_text_but_subject(blob, pat)
    if isinstance(blob, dict):
        out = {}
        for k, v in blob.items():
            nk = _mask_text_but_subject(k, pat) if isinstance(k, str) else k
            if nk != k and nk in out:
                n = 2
                while f"{nk}#{n}" in out:
                    n += 1
                nk = f"{nk}#{n}"
            out[nk] = _mask_json_other_emails(v, keep, pat)
        return out
    if isinstance(blob, list):
        return [_mask_json_other_emails(v, keep, pat) for v in blob]
    return blob


def _safe_endpoint(url: str) -> str:
    """Reduce a vendor endpoint URL to scheme://host[:port] ONLY — dropping userinfo
    (`user:pass@`), path (a webhook-style `/hooks/<token>` secret — R2 LOW-B), query
    (`?apikey=...`) and fragment (HIGH-2). Vendor endpoints can embed credentials the
    save-time validator historically didn't reject, so the subject-access export must
    never ship them; host is all the context the subject needs. An IPv6 host is re-
    bracketed (urlsplit.hostname strips the brackets, so `[2001:db8::1]:8443` would
    otherwise reassemble malformed — R2 LOW-A). Fails CLOSED: an unparseable value, or any
    residual '@'/'?' after reconstruction, collapses to a redaction marker.

    R2 LOW-C (cosmetic, no special-casing needed): a percent-encoded userinfo (`a%40b`)
    can't smuggle a credential through — emitting scheme+host ONLY drops path/query and
    urlsplit still parses the real `@` delimiter, so only the host survives regardless."""
    if not url:
        return url
    from urllib.parse import urlsplit, urlunsplit

    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if ":" in host:  # IPv6 literal — urlsplit stripped the brackets
            host = f"[{host}]"
        if p.port:
            host = f"{host}:{p.port}"
        safe = urlunsplit((p.scheme, host, "", "", ""))  # scheme+host ONLY (no path)
    except ValueError:
        return "[redacted-endpoint]"
    return "[redacted-endpoint]" if ("@" in safe or "?" in safe) else safe


def _count_where(s: Session, model, column, value) -> int:
    return s.exec(select(func.count()).select_from(model).where(column == value)).one()


def _count_where_any(s: Session, model, columns, value) -> int:
    cond = columns[0] == value
    for c in columns[1:]:
        cond = cond | (c == value)
    return s.exec(select(func.count()).select_from(model).where(cond)).one()


def _scrub_json_jobs(s: Session, email: str, tomb: str) -> int:
    """Rewrite `email`->`tomb` everywhere it appears inside Job.payload /
    Job.result JSON (payload["actor"] and any email string in result). Returns
    the number of Job rows changed. Runs inside the caller's transaction."""
    return _scrub_json_columns(s, Job, (Job.payload, Job.result), email, tomb)


def _scrub_json_columns(s: Session, model, columns, email: str, tomb: str) -> int:
    """Anchored rewrite `email`->`tomb` in every string leaf AND dict key of the JSON
    `columns` of `model` (integer PK). Returns the number of rows changed. Runs inside the
    caller's transaction. One seam for every _JSON_CONTENT_SWEEP entry."""
    pat = _anchored(email)  # R2 item 4: anchored, so a LONGER co-tenant address is safe
    changed = 0
    cursor = 0
    while True:
        rows = s.exec(
            select(model)
            .where(model.id > cursor, _like_cond(columns, email))
            .order_by(model.id)
            .limit(_BATCH)
        ).all()
        if not rows:
            break
        for r in rows:
            cursor = r.id
            dirty = False
            for col in columns:
                cur = getattr(r, col.key)
                new = _replace_in_json(cur, pat, tomb)
                if new != cur:
                    # reassign the whole object so SQLAlchemy flags the JSON column dirty
                    setattr(r, col.key, new)
                    dirty = True
            if dirty:
                s.add(r)
                changed += 1
    return changed


def _replace_in_json(blob, pat, new: str):
    """Recursively rewrite ANCHORED matches of `pat` -> `new` in every string leaf of a
    JSON-ish structure (dict/list/str), returning a NEW structure. Non-string leaves pass
    through unchanged. `pat` is a compiled _anchored() regex, so the subject's address is
    rewritten only as a WHOLE address, never inside a longer co-tenant one (R2 item 4 —
    this closes the previously-documented Job-JSON substring over-reach wart, giving ALL
    substring paths ONE uniform anchored semantics)."""
    if isinstance(blob, str):
        return pat.sub(new, blob)
    if isinstance(blob, dict):
        # dict KEYS too (gate fix-gdpr round 1): a map keyed by address keeps no residue
        return {
            (pat.sub(new, k) if isinstance(k, str) else k): _replace_in_json(v, pat, new)
            for k, v in blob.items()
        }
    if isinstance(blob, list):
        return [_replace_in_json(v, pat, new) for v in blob]
    return blob


def _scrub_text_column(s: Session, model, column, email: str, tomb: str) -> int:
    """ANCHORED-substring rewrite `email`->`tomb` inside a TEXT column where the email is
    EMBEDDED in a larger string, not the whole value — KeyChangeLog.actor's composite
    4th grammar ("workflow:wf<id>:<locale> by <email>"), which an exact-match sweep
    misses (HIGH-1). The rewrite uses the ANCHORED regex (R2 MED-A blocker): a bare
    `.replace()` corrupts a THIRD party whose address CONTAINS the subject's
    (ann@corp.com inside joann@corp.com -> "jodeleted-user-1@...", or ann@corp.com.mx),
    which would both leak the mangled remnant AND destroy an unrelated actor's
    attribution. Anchoring rewrites the subject as a WHOLE address only; it still covers
    the bare `<email>` grammar (a whole-value match) in one pass. Returns the number of
    rows changed. Runs inside the caller's transaction."""
    pat = _anchored(email)
    changed = 0
    cursor = 0
    while True:
        rows = s.exec(
            select(model)
            .where(model.id > cursor, _like_cond([column], email))
            .order_by(model.id)
            .limit(_BATCH)
        ).all()
        if not rows:
            break
        for r in rows:
            cursor = r.id
            cur = getattr(r, column.key) or ""
            new = pat.sub(tomb, cur)
            if new != cur:
                setattr(r, column.key, new)
                s.add(r)
                changed += 1
    return changed


def _scrub_customer_settings(s: Session, email: str, tomb: str) -> int:
    """Rewrite the subject's email -> `tomb` inside Customer.settings JSON (HIGH-3).
    Contact emails live here under `primary_contact`/`technical_contact` (the 360 admin
    form) AND under ARBITRARY keys (POST /api/customers/{cid}/settings + the CLI both
    merge caller-named keys), so a key-path-only sweep would miss a renamed/custom contact
    key. Instead do a VALUE-LEVEL EXACT-match pass over every string leaf (leaf == email
    -> tomb): exact-match — not substring — so a free-text `notes` value that merely
    MENTIONS an address is left alone, matching the never-scan-free-text posture. Settings
    is a merge dict: other keys are preserved (only matching leaves change). Returns the
    number of Customer rows changed. Runs inside the caller's transaction."""
    changed = 0
    cursor = 0
    while True:
        rows = s.exec(
            select(Customer)
            .where(Customer.id > cursor, _like_cond([Customer.settings], email))
            .order_by(Customer.id)
            .limit(_BATCH)
        ).all()
        if not rows:
            break
        for c in rows:
            cursor = c.id
            new_settings = _replace_exact_in_json(c.settings, email, tomb)
            if new_settings != c.settings:
                c.settings = new_settings  # reassign whole dict -> JSON column dirty
                s.add(c)
                changed += 1
    return changed


def _replace_exact_in_json(blob, old: str, new: str):
    """Recursively replace string leaves that EXACTLY equal `old` with `new`, returning
    a NEW structure (dict/list/str). Unlike _replace_in_json (substring), this only
    touches a leaf that IS the address, never a leaf that merely contains it."""
    if isinstance(blob, str):
        return new if blob.lower() == old.lower() else blob  # case-insensitive (L3)
    if isinstance(blob, dict):
        return {k: _replace_exact_in_json(v, old, new) for k, v in blob.items()}
    if isinstance(blob, list):
        return [_replace_exact_in_json(v, old, new) for v in blob]
    return blob


_ERASE_SWEEP = [
    ("projects", Project, Project.created_by),
    ("documents", Document, Document.created_by),
    ("jobs", Job, Job.created_by),
    ("connections", Connection, Connection.created_by),
    ("language_sets", LanguageSet, LanguageSet.created_by),
    ("sync_logs", SyncLog, SyncLog.actor),
    ("memory_events", MemoryEvent, MemoryEvent.actor),
    ("memory_entries", MemoryEntry, MemoryEntry.created_by),
    ("memory_entry_adjudicators", MemoryEntry, MemoryEntry.adjudicated_by),
    ("memory_suggestion_creators", MemorySuggestion, MemorySuggestion.created_by),
    ("memory_suggestion_reviewers", MemorySuggestion, MemorySuggestion.reviewed_by),
    ("memory_suggestion_appliers", MemorySuggestion, MemorySuggestion.applied_by),
    ("workflow_step_assignees", WorkflowStep, WorkflowStep.assignee_email),
    ("key_image_uploaders", KeyImage, KeyImage.uploaded_by),
    ("delivery_empty_approvers", DeliveryEmptyApproval, DeliveryEmptyApproval.approved_by),
    ("databricks_runs", DatabricksExportRun, DatabricksExportRun.actor),
    ("tap_actions", TapAction, TapAction.initiator),
    ("content_types", ContentType, ContentType.created_by),
    ("project_tm_sources", ProjectTmSource, ProjectTmSource.created_by),
    ("project_glossaries", ProjectGlossary, ProjectGlossary.created_by),
    ("term_candidates", TermCandidate, TermCandidate.decided_by),
    ("workflow_groups", WorkflowGroup, WorkflowGroup.created_by),
    ("project_templates", ProjectTemplate, ProjectTemplate.created_by),
    ("segmentation_profiles", SegmentationProfile, SegmentationProfile.created_by),
    ("context_profiles", ContextProfile, ContextProfile.created_by),
    ("workflow_batches", WorkflowBatch, WorkflowBatch.created_by),
    ("workflow_runs", WorkflowRun, WorkflowRun.created_by),
    ("engine_profiles", EngineProfile, EngineProfile.created_by),
    ("routing_rules", RoutingRule, RoutingRule.created_by),
    ("segment_post_edits", SegmentPostEdit, SegmentPostEdit.actor),
    ("query_thread_authors", QueryThread, QueryThread.author),
    ("query_thread_assignees", QueryThread, QueryThread.assignee_email),
    ("query_thread_closers", QueryThread, QueryThread.closed_by),
    ("query_post_authors", QueryPost, QueryPost.author),
    ("lqa_findings", LQAResult, LQAResult.actor),
    ("department_budgets", DepartmentBudget, DepartmentBudget.created_by),
    ("eval_datasets", EvalDataset, EvalDataset.created_by),
    ("eval_candidates", EvalCandidate, EvalCandidate.created_by),
    ("eval_runs", EvalRun, EvalRun.created_by),
    ("vendors", Vendor, Vendor.created_by),
    # #277: attribution columns added after the 2026-08-17 survey. Each writer stamps the
    # acting user's email (data_export submit, project_budgets, quality_lens, the Advanced
    # tier, glossary concepts, SCIM group edits, gold-label import's labeler fallback).
    ("data_export_runs", DataExportRun, DataExportRun.actor),
    ("project_budgets", ProjectBudget, ProjectBudget.created_by),
    ("project_automations", ProjectAutomation, ProjectAutomation.created_by),
    ("custom_field_defs", CustomFieldDef, CustomFieldDef.created_by),
    ("project_field_values", ProjectFieldValue, ProjectFieldValue.updated_by),
    ("quality_lenses", QualityLens, QualityLens.created_by),
    ("quality_lens_examples", QualityLensExample, QualityLensExample.created_by),
    ("advanced_missions", AdvancedMission, AdvancedMission.created_by),
    ("advanced_runs", AdvancedRun, AdvancedRun.created_by),
    ("glossary_concepts", GlossaryConcept, GlossaryConcept.created_by),
    ("user_group_member_adders", UserGroupMember, UserGroupMember.added_by),
    ("quality_gold_item_labelers", QualityGoldItem, QualityGoldItem.labeler),
    ("quality_gold_label_labelers", QualityGoldLabel, QualityGoldLabel.labeler),
    # Project automation Part B (review gate 10-13): who made a notification channel or route.
    # NotifyChannel.created_by is also the creator-or-admin edit right (notify.can_modify);
    # after erase only an admin can change the channel, which is the safe direction.
    ("notify_channels", NotifyChannel, NotifyChannel.created_by),
    ("notify_routes", NotifyRoute, NotifyRoute.created_by),
]


_ERASE_EXPORT_SECTION = {
    "projects": "projects",
    "documents": "documents",
    "jobs": "jobs",
    "connections": "connections",
    "language_sets": "language_sets",
    "sync_logs": "sync_logs",
    "memory_events": "memory_events",
    "memory_entries": "memory_entries",
    "memory_entry_adjudicators": "memory_entries",
    "memory_suggestion_creators": "memory_suggestions",
    "memory_suggestion_reviewers": "memory_suggestions",
    "memory_suggestion_appliers": "memory_suggestions",
    "databricks_runs": "databricks_runs",
    "tap_actions": "tap_actions",
    "content_types": "content_types",
    "custom_field_defs": "custom_field_defs",
    "project_field_values": "project_field_values",
    "project_tm_sources": "project_tm_sources",
    "project_glossaries": "project_glossaries",
    "term_candidates": "term_candidates",
    "workflow_groups": "workflow_groups",
    "project_templates": "project_templates",
    "segmentation_profiles": "segmentation_profiles",
    "context_profiles": "context_profiles",
    "workflow_batches": "workflow_batches",
    "workflow_runs": "workflow_runs",
    "engine_profiles": "engine_profiles",
    "routing_rules": "routing_rules",
    "segment_post_edits": "segment_post_edits",
    "query_thread_authors": "query_threads",
    "query_thread_assignees": "query_threads",
    "query_thread_closers": "query_threads",
    "query_post_authors": "query_posts",
    "lqa_findings": "lqa_findings",
    "department_budgets": "department_budgets",
    "eval_datasets": "eval_datasets",
    "eval_candidates": "eval_candidates",
    "eval_runs": "eval_runs",
    "vendors": "vendors",
    "key_image_uploaders": "key_images",
    "delivery_empty_approvers": "delivery_empty_approvals",
    "data_export_runs": "data_export_runs",
    "project_budgets": "project_budgets",
    "project_automations": "project_automations",
    "quality_lenses": "quality_lenses",
    "quality_lens_examples": "quality_lens_examples",
    "advanced_missions": "advanced_missions",
    "advanced_runs": "advanced_runs",
    "glossary_concepts": "glossary_concepts",
    "user_group_member_adders": "user_group_members",
    "quality_gold_item_labelers": "quality_gold_items",
    "quality_gold_label_labelers": "quality_gold_labels",
    "notify_channels": "notify_channels",
    "notify_routes": "notify_routes",
}


_EXPORT_EXEMPT = {
    "workflow_step_assignees": "WorkflowStep.assignee_email is a workflow-DEFINITION config field (the "  # noqa: E501
    "review-pause notice target), not the subject's own authored data; it is swept "
    "ONLY because it is a live SMTP send target, so it has no Art.15 export section.",
}


# --- #277 schema ratchet. verify_gdpr enumerates EVERY SQLModel table column that can hold
#     a user identity (a string or JSON column named email / actor / author / owner /
#     initiator / assignee / labeler / ..., or ending in `_by`; the rule lives in the suite)
#     and fails unless the column is in _ERASE_SWEEP, in _IDENTITY_SWEPT_ELSEWHERE (a
#     dedicated erase_user code block), or in _IDENTITY_NOT_SWEPT with a written reason.
#     Keys are "<table>.<column>" as SQLAlchemy names them. A new *_by column therefore
#     cannot ship without a decision.
_IDENTITY_SWEPT_ELSEWHERE = {
    "assistallowance.actor_hash": "erase_user deletes the subject's install-keyed HMAC bucket.",
    "user.email": "erase_user scrubs the User row itself (email -> tombstone).",
    "glossary.approved_by": "two-column Glossary/StyleGuide block in erase_user.",
    "glossary.created_by": "two-column Glossary/StyleGuide block in erase_user.",
    "styleguide.approved_by": "two-column Glossary/StyleGuide block in erase_user.",
    "styleguide.created_by": "two-column Glossary/StyleGuide block in erase_user.",
    "keychangelog.actor": "anchored substring scrub (_scrub_text_column), composite grammar.",
    "invite.email": "Invite two-column block in erase_user.",
    "invite.created_by": "Invite two-column block in erase_user.",
    "projectmember.added_by": "ProjectMember block in erase_user (added_by + own rows).",
    "customer.settings": "value-level exact (case-insensitive) sweep of every string leaf "
    "(_scrub_customer_settings); exported narrowed to the matching keys.",
}

_IDENTITY_NOT_SWEPT = {
    "systempromptoverride.created_by": (
        "Append-only version history of an org-level system prompt head (#356): the editing admin "
        "is kept as the attribution of an administrative configuration change, like the audit "
        "chain it is mirrored in (system_prompt.* rows); the row carries prompt text the org "
        "authored, never the subject's personal content."
    ),
    "workflowsystempromptoverride.created_by": (
        "Append-only version history of one workflow's own system prompt head (#356 phase 2b): "
        "the editing user is kept as the attribution of an administrative configuration change, "
        "like the audit chain it is mirrored in (workflow.system_prompt_* rows); the row carries "
        "prompt text the editor authored, never the subject's personal content."
    ),
    "auditlog.actor": "Append-only keyed hash chain (audit.verify_chain); retention is a "
    "legal obligation, GDPR Art. 17(3)(b). Export masks third parties instead.",
    "databricksexportrun.actor_hash_key_version": "The version label of the key used to "
    "pseudonymize actors in a lakehouse export; never a person.",
    "querythread.author_kind": "Enum naming the KIND of author (human/vendor).",
    "querypost.author_kind": "Enum naming the KIND of author (human/vendor).",
    "segmentpostedit.actor_kind": "Enum naming the KIND of editor (human/vendor).",
    "querypost.author_label": "Vendor-side display name/role copied verbatim from a vendor "
    "return, never a User of this install (models.py); vendor free text.",
    "userlayout.owner": "'user:<id>' key, not an email; erase_user deletes the subject's "
    "layouts outright (page_layouts.delete_for_user).",
    "workflowbatch.dispatcher_owner": "{host, pid} of the dispatching process, not a person.",
}


# --- gate fix-gdpr round 1 (M2): JSON columns can carry an address by CONTENT whatever
#     their name, so the ratchet classifies EVERY JSON column: swept by content here,
#     handled in _IDENTITY_SWEPT_ELSEWHERE (customer.settings), or in _JSON_NO_IDENTITY
#     with a reason. Each content-swept entry is (banner-label, model, columns, export)
#     where `export` names the export_user section that carries it back (Art.15) or starts
#     with "exempt:" and gives the reason.
_JSON_CONTENT_SWEEP = [
    ("job_json", Job, (Job.payload, Job.result), "jobs"),
    (
        "job_queue_payloads",
        JobQueue,
        (JobQueue.payload,),
        "exempt: the durable queue ledger's copy of Job.payload for the same job_id "
        "(ports/jobqueue.py enqueue); the Job row is exported under 'jobs'.",
    ),
    ("dataset_item_meta", EvalDatasetItem, (EvalDatasetItem.meta,), "dataset_item_labels"),
    # Project automation Part B (review gate 10-13): an email channel's recipient list is a
    # live SMTP send target, so an erased address must stop receiving notifications.
    (
        "notify_channel_recipients",
        NotifyChannel,
        (NotifyChannel.config,),
        "notify_channel_recipients",
    ),
    (
        "notify_outbox_payloads",
        NotifyOutbox,
        (NotifyOutbox.payload,),
        "exempt: the delivery log's `_sent_to` retry bookkeeping (recipients one message "
        "already reached), pruned after 30/90 days; the channel's own recipient list is "
        "exported under notify_channel_recipients.",
    ),
]

_R_CFG = "Configuration (tool/model/locale/step/check lists), written by product code; no person."
_R_CNT = "Counters, totals, watermarks or metrics; numbers keyed by entity or stream names."
_R_IDS = "Row ids or locale-to-id maps; no address."
_R_OPS = (
    "Operator- or content-typed JSON (connector/vendor config, translation context, "
    "document structure, segment/TM/glossary metadata, rework notes, frozen dispatch "
    "packages). No product write path stamps a user's address here; the KNOWN-RESIDUE "
    "free-text posture in gdpr.py applies."
)
_R_RUN = (
    "Run/transcript digests (provider, model, step and status fields); attribution lives "
    "in the row's own created_by/initiator column, which is swept."
)
# #617 item 7 (ADR 0062): the per-target-language MT formality maps on Customer, Project
# and Document ({"de": "formal"}). Locale codes to register names, set by an admin or manager.
_R_FORMALITY = (
    "MT formality configuration: target locale to register (formal/informal/auto or a "
    "language-specific register). No person; who changed it is in the audit log."
)

_JSON_NO_IDENTITY = {
    "notifyroute.events": _R_CFG,
    "advancedmission.tool_allowlist": _R_CFG,
    "advancedmission.model_roster": _R_CFG,
    "advancedmission.scripted_actions": _R_CFG,
    "advancedmission.requested_fences": _R_CFG,
    "advancedtranscriptentry.payload": _R_RUN,
    "connection.config": _R_OPS,
    "projectautomation.times": _R_CFG,
    "projectautomation.days": _R_CFG,
    "projectautomation.scope": _R_CFG,
    "projectautomation.full_cursor": _R_IDS,
    "contextprofile.context": _R_OPS,
    "customer.active_pairs": _R_CFG,
    "customer.formality": _R_FORMALITY,
    "databricksexportrun.streams": _R_CNT,
    "databricksexportrun.row_counts": _R_CNT,
    "databricksexportrun.dropped_counts": _R_CNT,
    "databricksexportrun.high_water": _R_CNT,
    "databricksexportrun.watermark_lag": _R_CNT,
    "dataexportrun.emitters": _R_CFG,
    "dataexportrun.entity_counts": _R_CNT,
    "dataexportrun.dropped_counts": _R_CNT,
    "deliveryemptyapproval.covered": _R_IDS,
    "document.skeleton": _R_OPS,
    "document.context": _R_OPS,
    "document.formality": _R_FORMALITY,
    "engineprofile.config": _R_CFG,
    "evalcandidate.config": _R_CFG,
    "evalrun.scorers": _R_CFG,
    "evalrun.metric_signature": _R_CFG,
    "evalscore.detail": _R_CNT,
    "glossaryconcept.cross_refs": _R_IDS,
    "glossaryentry.meta": _R_OPS,
    "glossaryentry.meta_overrides": _R_OPS,
    "languageset.target_locales": _R_CFG,
    "memorysuggestion.proposed_change": "Memory content patch; its three attribution "
    "columns (created_by/reviewed_by/applied_by) are swept.",
    "memorysuggestion.applied_snapshot": "Snapshot of memory content; attribution columns "
    "are swept.",
    "projecttemplate.target_locales": _R_CFG,
    "project.formality": _R_FORMALITY,
    "qualitylens.dimensions": _R_CFG,
    "qualitylens.severities": _R_CFG,
    "qualitylens.weights": _R_CFG,
    "qualitylens.checks": _R_CFG,
    "qualitylens.blocking_checks": _R_CFG,
    "qualitylens.assertions": _R_CFG,
    "segment.inline_tags": _R_OPS,
    "segment.qa_flags": _R_OPS,
    "segment.meta": _R_OPS,
    "segmenttm.meta": _R_OPS,
    "stringkey.tags": _R_OPS,
    "stringkey.custom": _R_OPS,
    "tapaction.requested_locales": _R_CFG,
    "tapaction.manifest": _R_RUN,
    "tapaction.totals": _R_CNT,
    "taprun.detail": _R_RUN,
    "vendor.config": _R_OPS,
    "vendor.rate_card": _R_OPS,
    "vendordispatch.estimate": _R_CNT,
    "vendordispatch.contract_ids": _R_IDS,
    "vendordispatch.retry_contract": _R_OPS,
    "vendordispatch.rework_feedback": _R_OPS,
    "workflowbatch.requested_locales": _R_CFG,
    "workflowbatch.reopened_members": _R_IDS,
    "workflowrun.context": _R_OPS,
    "workflowrun.formality": (
        "The MT formality register stamped on the run (register, document/project/customer "
        "level it came from, target locale, label); configuration, no person. Attribution "
        "is WorkflowRun.created_by, which is swept."
    ),
    "workflowstep.tm_pool_selection": _R_CFG,
    "workflowstep.run_condition": _R_CFG,
    "workflowstep.branch": _R_CFG,
}


# --- the exact-match tombstone sweep, as a MODULE CONSTANT (the Art.15/Art.17 symmetry
#     ratchet, gate M4). Each row is (banner-label, model, column). Lifted out of
#     erase_user so a test can enumerate it and assert every swept column has an EXPORT
#     counterpart (or a declared exemption) — the generalization of Wave 2's gate M2, so a
#     future wave cannot add a swept column with no export. The two-column (Glossary/
#     StyleGuide), substring (KeyChangeLog), JSON (Job/Customer) and membership (Invite/
#     ProjectMember) sweeps are separate code blocks with their own export sections and are
#     intentionally NOT in this list.
# LLMemory Phase 2 §5.6: resolve_conflict stamps the adjudicating human's email on the
# winner. A NEW actor-email column that would otherwise escape erasure silently.
# Phase-3 Train A: a MemorySuggestion carries THREE actor-email columns (created_by is
# "system" for generated rows, but reviewed_by/applied_by are operator emails). All three
# are swept exact-email->tombstone and exported together under one "memory_suggestions"
# section (the QueryThread three-column-to-one-section pattern).
# Batch 14: the uploader of a per-string in-context image. Exact-email -> tombstone
# (attribution only -- erase never deletes the image, whose blob is reaped by project
# delete / the retention backstop). Exported together under one "key_images" section.
# Batch 15 T2: the approver of a TM-only empty delivery. Exact-email -> tombstone
# (attribution only), exported under one "delivery_empty_approvals" section.
# Wave 3 (SPEC-wave3-collaboration §3.6/§6.1): the query entity's four attribution
# columns + the reviewer-finding actor. QueryThread.assignee_email is a LIVE SMTP send
# target (§4.6), exactly like WorkflowStep.assignee_email — the .invalid tombstone
# makes any future send a no-op. QueryPost.body / QueryThread.subject are EXPORTED but
# not scanned here (erase rewrites attribution, never third parties' free text).
# Every swept label maps to the export_user SECTION that carries the SAME data back
# (Art.15), so a test can prove symmetry. Several sweep columns fan into one section (the
# three QueryThread columns all export under "query_threads").
# Batch 14: the image-uploader sweep exports the subject's uploaded image METADATA
# (never bytes) under the org-less "key_images" section (KeyImage has no org_id --
# project/key-scoped, like KeyChangeLog).
# Batch 15 T2: the subject's empty-delivery approvals export under this section.
# Swept columns with NO Art.15 export counterpart, each with the reason it is exempt. A
# swept column is exported OR exempt — never silently dropped (M4).
def erase_user(uid: int, actor: str, ip: str = "", caller_org_id: int | None = None):
    """Anonymize a user: deactivate + scrub the User row, revoke API keys, and
    rewrite each ENUMERATED denormalized email pointer to a per-user tombstone
    (see the module docstring for the swept set AND the known-unswept remainder).
    The audit log is left intact (append-only; a single erase event is logged). Returns
    (ok, msg). Refuses self-erasure and refuses erasing the last active admin.

    All email-bearing writes (User scrub, denormalized column sweep, KeyChangeLog
    substring scrub, Job JSON scrub, Customer.settings scrub, ProjectMember removal,
    Invite rewrites) happen in ONE transaction with a single commit: a crash rolls the
    scrub back too, so a clean full re-run is always possible. The email is captured
    BEFORE any mutation.

    Multi-org P8: when `caller_org_id` is set (the customer /users/{uid}/erase route
    + the CLI `--org` scope pass the caller's org), an org-admin can only erase a
    user IN THEIR OWN org — a cross-org target is refused as "User not found." (same
    message as a truly-missing user, so a scoped admin can't even probe another org's
    ids). None (the operator / single-tenant path) is unscoped, byte-identical.

    DELIBERATE ASYMMETRY (R1 LOW-B) — export is org-SCOPED, erase is org-GLOBAL. Once a
    scoped caller is authorized to erase THIS subject (the cross-org refusal above passed),
    the sweep itself rewrites the subject's attribution in EVERY org, not just the caller's.
    That is correct under Art. 17's totality-of-erasure duty: the erasure must reach all of
    the subject's personal data, and a cross-org attribution pointer is still the subject's
    email. Export is the opposite (org-confined) because a subject-access COPY must not
    spill another org's rows to a scoped admin — disclosure and erasure have opposite
    default directions, on purpose. (The org gate is an AUTHORIZATION check on who may act,
    not a SCOPE limit on how far a lawful erasure reaches.)"""
    from app.services.admin_invariant import lock_user_mutations

    # Serialize before reading identity, scope or role, through the entire scrub.
    # Erasure, deactivation, demotion and org moves all acquire the same DB lock.
    counts = {}
    with Session(engine) as s:
        lock_user_mutations(s)
        u = s.get(User, uid)
        if not u:
            return False, "User not found."
        if caller_org_id is not None and u.org_id != caller_org_id:
            return False, "User not found."
        if actor and actor == u.email:
            return False, "You cannot erase your own account."

        email = u.email
        subject_org = u.org_id
        subject_cust = u.customer_id
        tomb = _tombstone(uid)
        if u.role == Role.ADMIN and u.active:
            # Recheck under this transaction (TOCTOU guard), through the SAME seam
            # set_role and set_active use. #178-1: it counts the SUBJECT's own org under
            # an active gate, whoever the caller is. The caller-org count it replaces
            # fell back to install-global for the local operator (caller_org_id None),
            # so org A's last admin could be erased because org B still had one. With
            # the gate off the count stays install-global, as before.
            from app.services import security

            if security.is_last_admin_of_own_org(s, uid):
                # Refused BEFORE any side effect — no key revocation, no session
                # kill, no writes. A refused erase must be provably a no-op (#21:
                # this recheck used to run AFTER the key revoke below, so a refused
                # last-admin erase still silently killed the admin's live API keys).
                return False, "Cannot erase the last active administrator."

        # (b) Revoke keys in THIS transaction, after the guard. A separate writer
        # would deadlock on SQLite's reservation; a failed scrub must roll back
        # the revocation too.
        # This seam has already authorized the subject; None is its operator scope.
        keys_revoked = apikeys.revoke_all_for_user(
            uid, org_id=access.UNSCOPED if caller_org_id is None else caller_org_id, session=s
        )

        # (a) instant session kill (session_user re-checks u.active every request)
        #     + (c) scrub the User row.
        row = s.get(User, uid)
        if not row:
            return False, "User not found."
        row.active = False
        from app.models import AssistAllowance
        from app.services import assist_limits

        allowance = s.get(AssistAllowance, assist_limits.actor_hash(email))
        if allowance is not None:
            s.delete(allowance)
        row.email = tomb
        row.name = ""
        row.password_hash = ""
        row.password_changed_at = None
        row.password_must_change = False
        # identity-01: drop the OIDC subject binding, so the erased person's IdP identity
        # no longer resolves to the tombstone row.
        row.sso_issuer = None
        row.sso_subject = None
        s.add(row)
        # Password policy: the account's previous-password hashes (the reuse history)
        # are credential material about the subject; erase deletes every row, in this
        # same transaction. (Export never includes them, like password_hash.)
        from app.services import password_policy

        password_policy.delete_history(s, uid)
        # TOTP train: the account's second factor (the encrypted authenticator secret and
        # the recovery-code hashes) is credential material about the subject; erase deletes
        # it in this same transaction. (Export never includes it.)
        from app.services import mfa

        mfa.delete_for_user(s, uid)

        # (d) rewrite every denormalized email pointer to the tombstone. Counts are
        #     taken BEFORE each UPDATE so the banner reports what actually moved.
        # llmemory's episode/belief layer denormalizes the actor email the same
        # way SyncLog does: MemoryEvent.actor (the captured episode's actor) and
        # MemoryEntry.created_by (the belief's author). Both are swept with the
        # identical exact-email->tombstone treatment. MemoryEvidence carries no
        # identity column (memory_id/event_id/created_at only), so it needs no
        # sweep. MemoryEvent's before_text/after_text/note/span are human-authored
        # CONTENT, not attribution, and are deliberately left unscanned — matching
        # the existing posture, which never scans free-text columns (Project.name,
        # Document.filename, ...) for embedded emails.
        #
        # WorkflowStep.assignee_email is a LIVE SMTP send target: notifications.py
        # mails the review-pause notice to it, so an un-swept value keeps emailing an
        # erased user after erasure. Rewriting it to the tombstone (whose .invalid
        # domain is a reserved never-deliverable TLD) turns any future send into a
        # harmless no-op. DatabricksExportRun.actor + TapAction.initiator are the same
        # attribution class as SyncLog.actor (a run's triggering actor) with no
        # hash-chain justification for exclusion, so they are swept too.
        #
        # actor_hash fork (Phase-2 Databricks exporter, informational): before this
        # sweep MemoryEvent.actor (and the other attribution columns) retained the raw
        # email, so the exporter hashed it to a STABLE pseudonym that survived erasure.
        # Rewriting actor to the tombstone here means the exporter now hashes the
        # tombstone instead — the pseudonym intentionally does NOT survive an Art.17
        # erasure, for EXPORTS RUN AFTER THE ERASURE. Rows already shipped to the
        # lakehouse are NOT reachable (an append-only copy cannot see a source row
        # change); that residual is honestly recorded per-run via
        # DatabricksExportRun.deletions_not_tracked. Correct.
        # WIDENING (task_3c57b418): the further attribution columns below join the
        # sweep with the IDENTICAL exact-email->tombstone treatment. Each row is one
        # (banner-label, model, column); a model with two attribution columns
        # (Glossary/StyleGuide: approved_by + created_by) gets two rows with distinct
        # labels. WorkflowRun.created_by is a LIVE SMTP send target (see the module
        # docstring) — tombstoning it stops an erased run-owner from being emailed.
        # L2 (issue #54): count only rows whose value actually CHANGES. On a RE-erase the
        # user's email is ALREADY the tombstone (email == tomb — the first erase set it), so
        # `column == email` matches the rows tombstoned by the FIRST erase and the banner
        # reported them as fresh rewrites (each a tomb->tomb no-op). An exact-email->tomb
        # rewrite moves a matched row iff email != tomb; when email == tomb nothing changes,
        # so the honest count is 0. When email != tomb this is byte-identical to before (a
        # `column == email` match can never already be the tombstone). The substring/JSON
        # scrubs below already count actual changes (`if new != cur`), so they need no guard;
        # memberships_removed keys on user_id (already deleted on re-erase) -> 0 naturally.
        _changed = (lambda n: 0) if email == tomb else (lambda n: n)
        for label, model, column in _ERASE_SWEEP:
            counts[label] = _changed(_count_where(s, model, column, email))
            s.exec(update(model).where(column == email).values({column.key: tomb}))

        # Glossary + StyleGuide each carry the subject in TWO columns. Count the UNION
        # once (LOW-1: a plain per-column count double-counts a row where the subject is
        # both approver AND creator; _count_where_any de-dups by id, mirroring Invite),
        # then rewrite each column.
        for label, model, cols in [
            ("glossaries", Glossary, (Glossary.approved_by, Glossary.created_by)),
            ("style_guides", StyleGuide, (StyleGuide.approved_by, StyleGuide.created_by)),
        ]:
            counts[label] = _changed(_count_where_any(s, model, cols, email))
            for col in cols:
                s.exec(update(model).where(col == email).values({col.key: tomb}))

        # (d1) KeyChangeLog.actor: SUBSTRING rewrite (HIGH-1 composite 4th grammar
        #      "workflow:wf<id>:<locale> by <email>" embeds the email; exact-match misses
        #      it). One pass also covers the bare `<email>` grammar.
        counts["key_change_logs"] = _scrub_text_column(
            s, KeyChangeLog, KeyChangeLog.actor, email, tomb
        )

        # (d2) email hides inside Job JSON (payload["actor"] / result) too.
        counts["job_json"] = _scrub_json_jobs(s, email, tomb)
        # (d2b) every OTHER JSON column that can carry an address by content (gate
        #       fix-gdpr round 1, M2): the job-queue ledger copy of Job.payload, the
        #       rules-labeler stamp in dataset item meta. Same anchored rewrite.
        for label, model, cols, _export in _JSON_CONTENT_SWEEP:
            if label != "job_json":
                counts[label] = _scrub_json_columns(s, model, cols, email, tomb)

        # (d3) Customer.settings JSON holds contact emails (primary_contact /
        #      technical_contact + arbitrary /api-merged keys) — value-level exact-match
        #      sweep (HIGH-3).
        counts["customer_settings"] = _scrub_customer_settings(s, email, tomb)

        # (e) ProjectMember: rewrite added_by (email) AND delete the subject's own
        #     membership rows (user_id == uid) — the export treats those as the
        #     subject's data, so erasure must remove them.
        counts["project_members"] = _changed(
            _count_where(s, ProjectMember, ProjectMember.added_by, email)
        )
        s.exec(update(ProjectMember).where(ProjectMember.added_by == email).values(added_by=tomb))
        counts["memberships_removed"] = _count_where(s, ProjectMember, ProjectMember.user_id, uid)
        s.exec(sa_delete(ProjectMember).where(ProjectMember.user_id == uid))

        # (e2) Train L1: the subject's own saved page layouts (a UI preference keyed to
        #      user_id / owner "user:<id>") are the subject's rows, so erasure removes
        #      them, in this same transaction, like the membership rows above.
        from app.services import page_layouts as _page_layouts

        counts["page_layouts_removed"] = _page_layouts.delete_for_user(s, uid)

        # (f) Invite carries the email in two columns — count the union, rewrite both.
        counts["invites"] = _changed(
            _count_where_any(s, Invite, (Invite.email, Invite.created_by), email)
        )
        s.exec(update(Invite).where(Invite.email == email).values(email=tomb))
        s.exec(update(Invite).where(Invite.created_by == email).values(created_by=tomb))

        s.commit()

    # (g) AuditLog is UNTOUCHED (append-only). Log a single erase event. The
    #     original email is NOT recorded (even a truncated sha256 of an email is
    #     reversible); a non-reversible reference (uid + erase timestamp) lets the
    #     trail be reconciled without re-exposing the address.
    ref = f"uid={uid} erased_at={datetime.utcnow().isoformat()}Z"
    audit.log(actor=actor, action="user.erase", target=tomb, detail=ref, ip=ip)

    # DX-207/DX-311: EXPORTS_DIR sits outside every existing erasure sweep, so a staged full-dump
    # export containing this subject would survive. Purge live staged artifacts whose scope covers
    # the subject. A user carries no project/customer of their OWN, so this matches on the user's
    # ORG (every live workspace-scope artifact in it -- deliberate fail-safe over-deletion) plus,
    # for a customer-bound user, any dump clamped to that customer. Org-confined -- never another
    # org's artifacts. Best-effort: a purge failure never fails the (completed) erasure.
    try:
        from app.services import data_export as _dx

        # #539: also purge the project and customer exports the SUBJECT
        # requested (matched by requester email or tombstone), whatever their scope; another
        # user's project/customer export in the same org is NOT touched.
        _dx.purge_artifacts_covering(
            org_id=subject_org,
            customer_ids={subject_cust} if subject_cust else set(),
            actors={email, tomb},
            reason=f"erase_user uid={uid}",
            actor=actor,
        )
    except Exception:
        _log.warning("gdpr.erase_user: staged-export artifact purge failed (uid=%s)", uid)

    parts = ", ".join(f"{v} {k}" for k, v in counts.items() if v) or "no linked records"
    msg = f"Erased user (anonymized). Revoked {keys_revoked} API key(s); rewrote {parts}."
    return True, msg


# --------------------------------------------------------------------------------------
# DX-207 staged-export artifact purge helpers (Batch 13, Train C). The document-erase
# (retention.delete_document) and customer-erase (customers.delete_customer) paths call THESE
# so that `app.services.data_export` is imported ONLY here + in scheduler.py (the two
# cross-feature reaches declared in the data_export Module's cross_reach_allow). Each resolves
# the erased subject's org + customer/project scope, then delegates to
# data_export.purge_artifacts_covering (org-confined, fail-safe over-deletion, LAW8 audit-kept).
# --------------------------------------------------------------------------------------
def purge_export_artifacts_for_document(project_id, *, actor: str = "gdpr") -> None:
    """A document was erased -- purge live staged exports whose scope covers its project (the
    document's content lives under that project's customer, so a project/customer/workspace/self
    dump of it would carry the erased text). Resolves org + customer from the surviving project
    (the document row itself is already gone). Best-effort; never raises into the erase txn."""
    if project_id is None:
        return
    try:
        from app.services import data_export as _dx

        with Session(engine) as s:
            proj = s.get(Project, project_id)
        if proj is None:
            return
        _dx.purge_artifacts_covering(
            org_id=getattr(proj, "org_id", None),
            customer_ids={proj.customer_id} if proj.customer_id else set(),
            project_ids={project_id},
            reason=f"erase_document project={project_id}",
            actor=actor,
        )
    except Exception:
        _log.warning(
            "gdpr: staged-export artifact purge for document (project=%s) failed", project_id
        )


def mark_export_artifacts_for_project(session, project_id, *, org_id=None, customer_id=None):
    """files-03: the IN-TRANSACTION half of the document/project purge. Called by
    retention.delete_document and content.delete_project with THEIR open session, BEFORE their
    commit, so the content delete and the `purged` flip of every live export covering it
    (project, customer, workspace AND self scopes) commit together. Returns an opaque pending
    list for `finish_export_artifact_purge` (run after the commit). `org_id` / `customer_id`
    default to the project row's own (pass them when the caller is about to delete the row).
    Never raises: on any error it returns None and the caller's post-commit
    `purge_export_artifacts_for_document` sweep is the fallback (today's best-effort path)."""
    if project_id is None:
        return None
    try:
        from app.services import data_export as _dx

        if org_id is None or customer_id is None:
            proj = session.get(Project, project_id)
            if proj is not None:
                org_id = org_id if org_id is not None else getattr(proj, "org_id", None)
                customer_id = customer_id if customer_id is not None else proj.customer_id
        marked = _dx.mark_covering_purged(
            session,
            org_id=org_id,
            customer_ids={customer_id} if customer_id else set(),
            project_ids={project_id},
        )
        return {"org_id": org_id, "marked": marked, "project_id": project_id}
    except Exception:
        _log.warning("gdpr: in-transaction export purge mark failed (project=%s)", project_id)
        return None


def finish_export_artifact_purge(pending, *, actor: str = "gdpr") -> None:
    """Post-commit companion of `mark_export_artifacts_for_project`: remove the staged bytes of
    every run it flipped to `purged` and audit it. Best-effort; never raises."""
    if not pending or not pending.get("marked"):
        return
    try:
        from app.services import data_export as _dx

        _dx.finish_purge(
            pending["marked"],
            org_id=pending.get("org_id"),
            reason=f"erase_document project={pending.get('project_id')}",
            actor=actor,
        )
    except Exception:
        _log.warning("gdpr: post-commit export artifact removal failed")


def purge_export_artifacts_for_customer(customer_id, *, org_id=None, actor: str = "gdpr") -> None:
    """A customer was erased/deleted -- purge live staged exports clamped to it, plus every
    workspace dump of its org (which would contain the customer's content). ``org_id`` may be
    passed explicitly by a caller that has ALREADY deleted the Customer row (delete_customer):
    the row is gone, so its org can no longer be looked up. Best-effort."""
    if customer_id is None:
        return
    try:
        from app.services import data_export as _dx

        with Session(engine) as s:
            pids = {
                p
                for (p,) in s.exec(
                    select(Project.id).where(Project.customer_id == customer_id)
                ).all()
            }
            if org_id is None:
                cust = s.get(Customer, customer_id)
                org_id = getattr(cust, "org_id", None) if cust is not None else None
        _dx.purge_artifacts_covering(
            org_id=org_id,
            customer_ids={customer_id},
            project_ids=pids,
            reason=f"erase_customer customer={customer_id}",
            actor=actor,
        )
    except Exception:
        _log.warning("gdpr: staged-export artifact purge for customer=%s failed", customer_id)
