"""Common document CLI argument parsers and database resolution helpers.

Leaf module in the CLI document command group providing shared argument
validation and org-scoped document lookup across document lifecycle
and segment inspection subcommands.

Documented in: docs/architecture/translation-pipeline.md
"""

import argparse
import os

from ._common import (
    EXIT_USAGE,
    CliError,
    bounded_id,
)


def _doc_id(value: str, subcommand: str = "doc translate") -> int:
    """argparse type for a numeric document id. When the value isn't an integer -
    the classic mistake is passing a FILE PATH ('doc translate C:\\...\\x.txt')
    instead of the numeric id - raise a friendly ArgumentTypeError that tells the
    operator to upload first, rather than argparse's opaque 'invalid int value'.

    `subcommand` names the ACTUAL subcommand being invoked (e.g. 'doc show',
    'doc apply-tm') so the hint doesn't hard-code 'doc translate' regardless of
    which subcommand the operator actually ran (bug #10) - each registration below
    binds its own subcommand name via functools.partial.

    Delegates the in-range/positive check to bounded_id (bug #7) once the value is
    confirmed to look like a plain integer, so a huge id gets the same clean usage
    error here as every other id argument, instead of an OverflowError later at
    the SQLite binder."""
    try:
        int(value)
    except (TypeError, ValueError) as e:
        looks_like_path = (
            os.sep in value
            or (os.altsep and os.altsep in value)
            or os.path.splitext(value)[1] != ""
        )
        if looks_like_path:
            raise argparse.ArgumentTypeError(
                f"{subcommand} takes a numeric document id, not a file ({value!r}). "
                "First:  loc-tms doc upload <project_id> <file>  "
                "- then use the returned document id."
            ) from e
        raise argparse.ArgumentTypeError(f"document id must be a number, got {value!r}.") from e
    return bounded_id(value)


# --- helpers -----------------------------------------------------------------
def _get_doc(session, doc_id: int, org_id=None):
    """The ONE resolution every `doc` verb goes through — the CLI twin of
    `api_cli._visible_document` / `deps._visible_doc` (dual-surface law).

    A key project's INTERNAL documents are NOT FOUND here (SPEC-key-based-projects
    §2.5, fix round 2 W6). `doc list` already filtered them out of the listing, but
    every by-id verb took the raw row: `doc show <trash_id>` printed a soft-deleted
    key's whole parked catalogue with exit 0, and `doc apply-tm <trash_id>` WROTE to
    those rows — while both /api twins already answered 404. Spelled exactly like a
    missing id, so this is not an existence oracle either."""
    from app.kernel import keys as keys_svc
    from app.models import Document, Project

    doc = session.get(Document, doc_id)
    if doc is None:
        raise CliError(f"no such document: {doc_id}", EXIT_USAGE)
    if doc.fmt == keys_svc.KEY_DOC_FMT:
        raise CliError(f"no such document: {doc_id}", EXIT_USAGE)
    # In-process --org scoping: a doc whose project is in another org is 404.
    if org_id is not None:
        project = session.get(Project, doc.project_id)
        if project is None or getattr(project, "org_id", None) != org_id:
            raise CliError(f"no such document: {doc_id}", EXIT_USAGE)
    return doc


__all__ = [
    "_doc_id",
    "_get_doc",
]
