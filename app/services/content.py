"""Content operations shared by the web routes and the headless CLI.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports DUE_SOON_DAYS, _SQLITE_INT64_MAX, InvalidLocaleError,
ForeignCustomerError, org_has_selectable_customer, MissingCustomerError, ArchivedCustomerError,
ProjectDeleteBlocked, DueDateError, LocaleRemovalBlocked, InvalidContentTypeError,
InvalidProjectTypeError, available_project_types, _logger, parse_due_date, days_until_due,
due_status, set_project_content_type, _org_for, customer_create, _rollback_project_create,
ProjectCustomerAssignError, assign_project_customer, rename_project, ProjectArchiveError,
ProjectArchivedError, archive_project, unarchive_project, project_active_only, project_is_active,
refuse_if_project_archived, _project_job_ids, _CONTROL_CHARS_RE, _CD_UNSAFE_RE,
header_safe_filename, delete_document, _materialize_key_values, _seed_locale_segments,
_project_locale_norms, _project_locale_rows, translated_target_locales, _locale_query_thread_ids,
set_project_due, delete_project_preview, content_disposition_attachment, rename_document,
validate_document_filename, safe_document_filename, safe_output_stem, safe_output_name,
add_target_locale, remove_locale_preview, apply_language_set_to_project, project_create,
delete_project, remove_target_locale from app.services.content_project_rename_delete_sp,
app.services.content_part2, app.services.content_projects.

These functions hold the business logic that used to live INSIDE the route
handlers in app/web/projects.py and app/web/documents.py. The web routes now
call them; the CLI (app/cli/cmd_project.py, cmd_doc.py) calls them too, so there
is ONE code path for customer/project creation, applying a Language Set, and
applying 100%-TM matches to a document.

Each function is a pure service call: it takes a live Session (the caller owns
the transaction boundary via commit inside these helpers, matching the route
behaviour it replaced) and returns the created/affected object or a count. No
HTTP concerns (redirects, form parsing, visibility gates) leak in — those stay
in the routes.

Documented in: docs/architecture/content-and-projects.md
"""

import logging
import math
import re
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models import (
    Customer,
    Document,
    Project,
    ProjectPairProvider,
    ProjectStatus,
    ProjectType,
    ProviderMode,
    Segment,
    TargetLocale,
)
from app.services import access, ids, languages, languages_catalog, qa, quality, tm
from app.services.content_part2 import (
    active_target_locales,
    add_target_locale,
    apply_language_set_to_project,
    content_disposition_attachment,
    delete_project_preview,
    is_bound_project,
    remove_locale_preview,
    rename_document,
    safe_document_filename,
    safe_output_name,
    safe_output_stem,
    set_project_due,
    stopped_target_locales,
    validate_document_filename,
)
from app.services.content_project_rename_delete_sp import (
    _CD_UNSAFE_RE,
    _CONTROL_CHARS_RE,
    _SQLITE_INT64_MAX,
    DUE_SOON_DAYS,
    ArchivedCustomerError,
    DueDateError,
    DuplicateNameError,
    ForeignCustomerError,
    InvalidContentTypeError,
    InvalidLocaleError,
    InvalidProjectTypeError,
    LocaleRemovalBlocked,
    MissingCustomerError,
    ProjectArchivedError,
    ProjectArchiveError,
    ProjectCustomerAssignError,
    ProjectDeleteBlocked,
    UnknownCustomerError,
    _locale_query_thread_ids,
    _logger,
    _materialize_key_values,
    _org_for,
    _project_job_ids,
    _project_locale_norms,
    _project_locale_rows,
    _rollback_project_create,
    _seed_locale_segments,
    archive_project,
    assign_project_customer,
    available_project_types,
    customer_create,
    days_until_due,
    delete_document,
    due_status,
    find_duplicate_customer,
    find_duplicate_project,
    header_safe_filename,
    org_has_selectable_customer,
    parse_due_date,
    project_active_only,
    project_is_active,
    refuse_if_project_archived,
    rename_project,
    set_project_content_type,
    translated_target_locales,
    unarchive_project,
)
from app.services.content_projects import delete_project, project_create, remove_target_locale
from app.services.pair_providers import MockEngineAdminOnlyError, PairProviderError

__all__ = [
    "DUE_SOON_DAYS",
    "_CD_UNSAFE_RE",
    "_CONTROL_CHARS_RE",
    "_SQLITE_INT64_MAX",
    "ArchivedCustomerError",
    "Customer",
    "DueDateError",
    "DuplicateNameError",
    "ForeignCustomerError",
    "InvalidContentTypeError",
    "InvalidLocaleError",
    "InvalidProjectTypeError",
    "LocaleRemovalBlocked",
    "MissingCustomerError",
    "MockEngineAdminOnlyError",
    "PairProviderError",
    "ProjectArchiveError",
    "ProjectArchivedError",
    "ProjectCustomerAssignError",
    "ProjectDeleteBlocked",
    "ProjectPairProvider",
    "ProjectStatus",
    "ProjectType",
    "ProviderMode",
    "TargetLocale",
    "UnknownCustomerError",
    "_locale_query_thread_ids",
    "_logger",
    "_materialize_key_values",
    "_org_for",
    "_project_job_ids",
    "_project_locale_norms",
    "_project_locale_rows",
    "_rollback_project_create",
    "_seed_locale_segments",
    "access",
    "active_target_locales",
    "add_target_locale",
    "apply_language_set_to_project",
    "archive_project",
    "assign_project_customer",
    "available_project_types",
    "content_disposition_attachment",
    "customer_create",
    "datetime",
    "days_until_due",
    "delete_document",
    "delete_project",
    "delete_project_preview",
    "due_status",
    "find_duplicate_customer",
    "find_duplicate_project",
    "header_safe_filename",
    "ids",
    "is_bound_project",
    "languages",
    "logging",
    "math",
    "org_has_selectable_customer",
    "parse_due_date",
    "project_active_only",
    "project_create",
    "project_is_active",
    "re",
    "refuse_if_project_archived",
    "remove_locale_preview",
    "remove_target_locale",
    "rename_document",
    "rename_project",
    "safe_document_filename",
    "safe_output_name",
    "safe_output_stem",
    "set_project_content_type",
    "set_project_due",
    "stopped_target_locales",
    "timezone",
    "translated_target_locales",
    "unarchive_project",
    "validate_document_filename",
]


# --- apply TM to a document --------------------------------------------------
def apply_tm_to_document(session: Session, doc: Document, project: Project, locale: str) -> int:
    """Fill in target text for every segment with a 100% TM match.

    One batched fuzzy lookup for every segment's source instead of a per-segment
    tm.lookup() query pair; apply-tm only ever wants exact (100%) hits, so the
    plain (non-semantic) batch is enough here. QA still runs on the TM-applied
    target (the 100% short-circuit must not bypass placeholder/tag integrity);
    a mismatch is logged so it isn't silently applied. Returns the count of
    segments filled.

    Validates `locale` against the BCP-47 catalog first (bug #11): an unknown
    code like 'zz-ZZ' used to fall through to a query that simply never matches
    anything, silently returning 0 -- indistinguishable from a real, empty TM.
    Raising InvalidLocaleError here (the same error project_create/add_locale
    raise) gives both the web route and the CLI a clean, visible rejection
    instead of a quiet no-op.

    Grandfathered exception: a locale that fails catalog validation but is
    ALREADY a TargetLocale of this project is let through instead of raised.
    Legacy projects and connector-synced projects can carry non-catalog target
    locales (add_target_locale's comment above notes a connector's free-text
    config seeds TargetLocale unvalidated, e.g. "cy-GB"/"haw-US") -- apply-tm on
    those previously-working locales must keep working. Only a locale that is
    BOTH invalid AND unknown to the project (a genuine typo) still raises."""
    if not languages_catalog.is_valid(locale):
        existing_norm = _project_locale_norms(session, doc.project_id)
        if languages_catalog.normalize(locale) not in existing_norm:
            raise InvalidLocaleError([locale])
    # #617 item 4 (gate): a run parked on a human_review step holds this locale's text; a TM
    # fill now would be overwritten by (or fight) the resume. Web, /api, CLI and MCP all map it
    # to the same 409 `awaiting_review`.
    from app.services import segment_review

    segment_review.refuse_if_awaiting_review(session, doc.id, locale)
    segments = session.exec(
        select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == locale)
    ).all()
    applied = 0
    applied_tm_ids: list[int] = []  # SegmentTM entries whose target we APPLIED
    matches = tm.lookup_batch(
        session, doc.project_id, [s.source_text for s in segments], project.source_locale, locale
    )
    for s in segments:
        match = matches.get(s.source_text)
        if match and match["match_pct"] == 100 and not s.target_text:
            # SEAM FIX (Phase 0). Route the manual apply-tm 100% fill through the SOLE
            # sanctioned write seam (quality.commit_mt_output), like the pretranslate
            # and workflow paths, so a poisoned TM entry cannot land here as clean
            # SegStatus.MT either — every refusal/length/number/tag/meta gate applies.
            # tm_eligible=False (applying a stored entry never re-seeds TM).
            outcome = quality.commit_mt_output(
                session, s, match["target_text"], source="tm", tm_eligible=False
            )
            if outcome.flagged:
                # Refusal-shaped hit: withheld + routed to NEEDS_REVIEW, not an apply.
                continue
            s.tm_match_pct = 100
            session.add(s)
            applied += 1
            if match.get("id") is not None:
                applied_tm_ids.append(match["id"])
            flags = qa.check_segment(s.source_text, s.target_text)
            if any(f["type"] in ("placeholder", "tags") for f in flags):
                _logger.warning("apply-tm QA flags on segment %s: %s", s.id, flags)
    session.commit()
    # Usage tracking (TM module): one bulk UPDATE for every entry applied here.
    if applied_tm_ids:
        from app.services import tm_module

        # project-scoped: an APPLIED row that belongs to an ATTACHED source project
        # is deliberately not counted -- usage drives the owner's curation, and a
        # borrower's traffic must not move it (see bump_usage).
        tm_module.bump_usage(session, applied_tm_ids, project_id=doc.project_id)
    return applied
