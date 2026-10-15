"""`project` command group: customer / project / tm / glossary / langset.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _LIST_CAP, _list_window_args, _resolve_window,
_note_truncated, _TERM_META_FLAGS, _add_term_meta_args, _term_meta_from_args,
_CONCEPT_TERM_STR_FIELDS, _CONCEPT_TERM_BOOL_FIELDS, _add_concept_term_args, _concept_term_tdoc,
_parse_locale_surface_terms, _register_langset, _get_project, _read_bytes, _write_bytes,
_customer_create, _get_customer, _customer_show, _customer_update, _project_create, _project_author,
_tm_visible_pids, _tm_filters, _tm_row, _skipped_note, _changed_note, _gmod_get, _concept_payload,
_concept_terms, _langset_create, _langset_list, _langset_update, _langset_delete,
_customer_set_default, _customer_pairs_list, _customer_pairs_add, _customer_pairs_remove,
_customer_pairs_import, _customer_rename, _customer_delete, _customer_archive, _customer_unarchive,
_project_show, _project_leverage, _project_apply_langset, _project_add_locale,
_project_remove_locale, _project_status, _project_set_providers, _project_set_due,
_project_set_content_type, _project_set_customer, _project_tm_sources_list, _project_tm_sources_add,
_project_tm_sources_remove, _project_glossaries_list, _project_glossaries_add,
_project_glossaries_remove, _project_export, _project_rename, _project_archive, _project_unarchive,
_project_delete, _tm_import, _tm_export, _tm_list, _tm_search, _tm_show, _tm_edit, _tm_stats,
_tm_scrub_hedges, _tm_delete, _glossary_import, _glossary_export, _glossary_add, _gmod_create,
_gmod_list, _gmod_set_customer, _META_COL_ABBR, _meta_summary, _gmod_approve, _gmod_unapprove,
_register_tm, _customer_list, _gmod_set_pairs, _gmod_set_content_type, _gmod_import, _gmod_export,
_gmod_delete, _gmod_rename, _gmod_extract, _gmod_candidates_list, _gmod_candidate_accept,
_gmod_candidate_drop, _gmod_concepts_list, _gmod_concept_show, _gmod_concept_add,
_gmod_concept_edit, _gmod_concept_delete, _gmod_term_delete, _gmod_term_set_audience,
_register_customer, _project_list, _glossary_list, _gmod_show, _gmod_add_term, _gmod_edit_term,
_gmod_term_add, _gmod_term_edit, _register_project from app.cli.cmd_project_project,
app.cli.cmd_project_part2, app.cli.cmd_project_glossary,
app.cli.cmd_project_term_extraction_candidat, app.cli.cmd_project_part5, app.cli.cmd_project_part6.

All in-process (local-root, no RBAC — same trust as editing the DB directly).
Business logic lives in app.services.* (content.py, tm.py, glossary.py,
languages.py); this module is a thin CLI shell: parse args, open a Session, call
the service, and emit a friendly table (or --json).

P6: every command here is remote-capable — it dispatches to _remote_impl over /api
when --remote is set, else runs in-process (with the SaaS operator gate applied).

Documented in: docs/architecture/content-and-projects.md
"""

import os

from app.cli.cmd_project_glossary import (
    _META_COL_ABBR,
    _glossary_add,
    _glossary_export,
    _glossary_import,
    _gmod_approve,
    _gmod_create,
    _gmod_list,
    _gmod_set_customer,
    _gmod_unapprove,
    _meta_summary,
    _project_archive,
    _project_delete,
    _project_export,
    _project_glossaries_remove,
    _project_rename,
    _project_unarchive,
    _tm_delete,
    _tm_edit,
    _tm_export,
    _tm_import,
    _tm_list,
    _tm_scrub_hedges,
    _tm_search,
    _tm_show,
    _tm_stats,
)
from app.cli.cmd_project_part2 import (
    _customer_archive,
    _customer_delete,
    _customer_pairs_add,
    _customer_pairs_import,
    _customer_pairs_list,
    _customer_pairs_remove,
    _customer_rename,
    _customer_set_default,
    _customer_unarchive,
    _project_add_locale,
    _project_apply_langset,
    _project_glossaries_add,
    _project_glossaries_list,
    _project_leverage,
    _project_remove_locale,
    _project_set_content_type,
    _project_set_customer,
    _project_set_due,
    _project_set_providers,
    _project_show,
    _project_status,
    _project_tm_sources_add,
    _project_tm_sources_list,
    _project_tm_sources_remove,
)
from app.cli.cmd_project_part5 import (
    _glossary_list,
    _gmod_add_term,
    _gmod_edit_term,
    _gmod_show,
    _gmod_term_add,
    _gmod_term_edit,
    _project_list,
    _register_customer,
)
from app.cli.cmd_project_part6 import _register_project
from app.cli.cmd_project_project import (
    _CONCEPT_TERM_BOOL_FIELDS,
    _CONCEPT_TERM_STR_FIELDS,
    _LIST_CAP,
    _TERM_META_FLAGS,
    _add_concept_term_args,
    _add_term_meta_args,
    _changed_note,
    _concept_payload,
    _concept_term_tdoc,
    _concept_terms,
    _customer_create,
    _customer_show,
    _customer_update,
    _get_customer,
    _get_project,
    _gmod_get,
    _langset_create,
    _langset_delete,
    _langset_list,
    _langset_update,
    _list_window_args,
    _note_truncated,
    _parse_locale_surface_terms,
    _project_author,
    _project_create,
    _read_bytes,
    _register_langset,
    _resolve_window,
    _skipped_note,
    _term_meta_from_args,
    _tm_filters,
    _tm_row,
    _tm_visible_pids,
    _write_bytes,
)
from app.cli.cmd_project_term_extraction_candidat import (
    _customer_list,
    _gmod_candidate_accept,
    _gmod_candidate_drop,
    _gmod_candidates_list,
    _gmod_concept_add,
    _gmod_concept_delete,
    _gmod_concept_edit,
    _gmod_concept_show,
    _gmod_concepts_list,
    _gmod_delete,
    _gmod_export,
    _gmod_extract,
    _gmod_import,
    _gmod_rename,
    _gmod_set_content_type,
    _gmod_set_pairs,
    _gmod_term_delete,
    _gmod_term_set_audience,
    _register_tm,
)

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_cursor,
    bounded_id,
    confirm,
    get_scoped_or_404,
    op_creator,
    op_org_id,
    op_org_scope,
    projected_entries_sentence,
    remote_mode,
    require_local,
    require_org_scope,
)

__all__ = [
    "EXIT_USAGE",
    "_CONCEPT_TERM_BOOL_FIELDS",
    "_CONCEPT_TERM_STR_FIELDS",
    "_LIST_CAP",
    "_META_COL_ABBR",
    "_TERM_META_FLAGS",
    "CliError",
    "_add_concept_term_args",
    "_add_term_meta_args",
    "_changed_note",
    "_concept_payload",
    "_concept_term_tdoc",
    "_concept_terms",
    "_customer_archive",
    "_customer_create",
    "_customer_delete",
    "_customer_list",
    "_customer_pairs_add",
    "_customer_pairs_import",
    "_customer_pairs_list",
    "_customer_pairs_remove",
    "_customer_rename",
    "_customer_set_default",
    "_customer_show",
    "_customer_unarchive",
    "_customer_update",
    "_get_customer",
    "_get_project",
    "_glossary_add",
    "_glossary_export",
    "_glossary_import",
    "_glossary_list",
    "_gmod_add_term",
    "_gmod_approve",
    "_gmod_candidate_accept",
    "_gmod_candidate_drop",
    "_gmod_candidates_list",
    "_gmod_concept_add",
    "_gmod_concept_delete",
    "_gmod_concept_edit",
    "_gmod_concept_show",
    "_gmod_concepts_list",
    "_gmod_create",
    "_gmod_delete",
    "_gmod_edit_term",
    "_gmod_export",
    "_gmod_extract",
    "_gmod_get",
    "_gmod_import",
    "_gmod_list",
    "_gmod_rename",
    "_gmod_set_content_type",
    "_gmod_set_customer",
    "_gmod_set_pairs",
    "_gmod_show",
    "_gmod_term_add",
    "_gmod_term_delete",
    "_gmod_term_edit",
    "_gmod_term_set_audience",
    "_gmod_unapprove",
    "_langset_create",
    "_langset_delete",
    "_langset_list",
    "_langset_update",
    "_list_window_args",
    "_meta_summary",
    "_note_truncated",
    "_parse_locale_surface_terms",
    "_project_add_locale",
    "_project_apply_langset",
    "_project_archive",
    "_project_author",
    "_project_create",
    "_project_delete",
    "_project_export",
    "_project_glossaries_add",
    "_project_glossaries_list",
    "_project_glossaries_remove",
    "_project_leverage",
    "_project_list",
    "_project_remove_locale",
    "_project_rename",
    "_project_set_content_type",
    "_project_set_customer",
    "_project_set_due",
    "_project_set_providers",
    "_project_show",
    "_project_status",
    "_project_tm_sources_add",
    "_project_tm_sources_list",
    "_project_tm_sources_remove",
    "_project_unarchive",
    "_read_bytes",
    "_register_customer",
    "_register_langset",
    "_register_project",
    "_register_tm",
    "_remote_impl",
    "_resolve_window",
    "_session",
    "_skipped_note",
    "_term_meta_from_args",
    "_tm_delete",
    "_tm_edit",
    "_tm_export",
    "_tm_filters",
    "_tm_import",
    "_tm_list",
    "_tm_row",
    "_tm_scrub_hedges",
    "_tm_search",
    "_tm_show",
    "_tm_stats",
    "_tm_visible_pids",
    "_write_bytes",
    "confirm",
    "get_scoped_or_404",
    "op_creator",
    "op_org_id",
    "op_org_scope",
    "os",
    "projected_entries_sentence",
    "remote_mode",
    "require_local",
    "require_org_scope",
]


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    _register_customer(subparsers)
    _register_project(subparsers)
    _register_tm(subparsers)
    _register_glossary(subparsers)
    _register_langset(subparsers)


def _register_glossary(subparsers) -> None:
    gl = subparsers.add_parser("glossary", help="Glossary/termbase (import, add, list).")
    sub = gl.add_subparsers(dest="glossary_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("import", help="Import a glossary CSV or TBX into a project.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("csv", help="path to the .csv or .tbx file (format from the extension).")
    p.add_argument("--source", required=True, help="source locale.")
    p.add_argument("--target", required=True, help="target locale.")
    p.set_defaults(func=_glossary_import)

    p = sub.add_parser("add", help="Add a single glossary term.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("source_term", help="the source term.")
    p.add_argument("--target-term", default="", help="its translation.")
    p.add_argument("--source", default="", help="source locale (default: project's).")
    p.add_argument("--target", default="", help="target locale.")
    p.add_argument("--dnt", action="store_true", help="mark do-not-translate.")
    p.set_defaults(func=_glossary_add)

    p = sub.add_parser("list", help="List a project's glossary entries.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    _list_window_args(p)
    p.add_argument(
        "--cursor", type=bounded_cursor, default=None, help="last entry id from the previous page."
    )
    p.set_defaults(func=_glossary_list)

    p = sub.add_parser(
        "export",
        help="Export a project's glossary to CSV or TBX "
        "(round-trips import; includes the TBX-ish meta columns).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("csv", help="output path; .tbx writes TBX, otherwise CSV.")
    p.add_argument("--force", action="store_true", help="overwrite an existing file.")
    p.set_defaults(func=_glossary_export)

    # --- dedicated glossary MODULE (Phase 5-B): first-class glossaries with an
    # approval lifecycle + language-pair association + a workflow-step dropdown. ---
    mod = sub.add_parser(
        "module", help="Dedicated glossary module (create, approve, pairs, terms)."
    )
    msub = mod.add_subparsers(dest="glossary_module_cmd", metavar="<subcommand>")
    msub.required = True

    p = msub.add_parser("create", help="Create a draft glossary (belongs to a customer).")
    p.add_argument("name", help="glossary name.")
    p.add_argument(
        "--customer",
        type=bounded_id,
        default=None,
        required=True,
        help="customer id this glossary belongs to (required -- every "
        "glossary belongs to a customer).",
    )
    p.add_argument(
        "--content-type",
        dest="content_type_id",
        type=bounded_id,
        default=None,
        help="scope to a content type id (SPEC-content-types).",
    )
    p.set_defaults(func=_gmod_create)

    p = msub.add_parser("list", help="List glossaries.")
    p.add_argument(
        "--customer",
        type=bounded_id,
        default=None,
        help="show one customer's glossaries; omit for all in scope.",
    )
    p.add_argument(
        "--unassigned", action="store_true", help="show only legacy glossaries with no customer."
    )
    p.set_defaults(func=_gmod_list)

    p = msub.add_parser(
        "set-customer",
        help="Assign a legacy unassigned glossary to a customer (one-time; never a reassignment).",
    )
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("customer_id", type=bounded_id, help="customer id to assign to.")
    p.set_defaults(func=_gmod_set_customer)

    p = msub.add_parser("set-content-type", help="Set or clear a glossary's content type.")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument(
        "content_type_id",
        type=bounded_id,
        nargs="?",
        default=None,
        help="content type id (omit or use --clear to clear).",
    )
    p.add_argument("--clear", action="store_true", help="clear the content type (back to General).")
    p.set_defaults(func=_gmod_set_content_type)

    p = msub.add_parser("show", help="Show one glossary (status, pairs, terms).")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    _list_window_args(p)
    p.add_argument(
        "--cursor",
        type=bounded_cursor,
        default=None,
        help="last entry id (default/cap: 1000 terms).",
    )
    p.set_defaults(func=_gmod_show)

    p = msub.add_parser("approve", help="Approve a glossary.")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.set_defaults(func=_gmod_approve)

    p = msub.add_parser("unapprove", help="Revert a glossary to draft.")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.set_defaults(func=_gmod_unapprove)

    p = msub.add_parser("set-pairs", help="Set a glossary's language pairs (replaces).")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument(
        "--pair",
        action="append",
        default=[],
        metavar="SRC:TGT",
        help="a source:target pair (repeatable).",
    )
    p.set_defaults(func=_gmod_set_pairs)

    p = msub.add_parser("add-term", help="Add a term to a glossary.")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("source_term", help="the source term.")
    p.add_argument("--target-term", default="", help="its translation.")
    p.add_argument("--source", default="", help="source locale.")
    p.add_argument("--target", default="", help="target locale.")
    p.add_argument("--dnt", action="store_true", help="mark do-not-translate.")
    _add_term_meta_args(p)
    p.set_defaults(func=_gmod_add_term)

    p = msub.add_parser(
        "edit-term",
        help="Edit a term's target/DNT + details "
        "(definition, part of speech, usage, domain, notes).",
    )
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("entry_id", type=bounded_id, help="the term (entry) id.")
    p.add_argument("--target-term", default=None, help="new translation (unchanged if omitted).")
    dnt_grp = p.add_mutually_exclusive_group()
    dnt_grp.add_argument(
        "--dnt", dest="dnt", action="store_true", default=None, help="mark do-not-translate."
    )
    dnt_grp.add_argument(
        "--no-dnt", dest="dnt", action="store_false", help="clear do-not-translate."
    )
    cs_grp = p.add_mutually_exclusive_group()
    cs_grp.add_argument(
        "--case-sensitive",
        dest="case_sensitive",
        action="store_true",
        default=None,
        help="match this pair case-sensitively.",
    )
    cs_grp.add_argument(
        "--no-case-sensitive",
        dest="case_sensitive",
        action="store_false",
        help="match this pair case-insensitively.",
    )
    _add_term_meta_args(p, edit=True)
    p.set_defaults(func=_gmod_edit_term)

    p = msub.add_parser("import", help="Import a CSV, TBX, or .glossary.json into a glossary.")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument(
        "csv",
        help="path to the .csv, .tbx, or .glossary.json file (format from the "
        "extension). --source/--target are ignored for .glossary.json.",
    )
    p.add_argument("--source", required=True, help="source locale (CSV/TBX only).")
    p.add_argument("--target", required=True, help="target locale (CSV/TBX only).")
    p.add_argument(
        "--strict",
        action="store_true",
        help="TBX only: refuse (write nothing) on the first TBX-Basic conformance finding "
        "(Decision D). Inert for CSV / .glossary.json.",
    )
    p.add_argument(
        "--concepts",
        action="store_true",
        help="TBX only, RESERVED: the concept-PRESERVING import is deferred to the terminology "
        "authority train; this flag is refused (write nothing) with a message pointing at the "
        "standard import. Use the default flat entry import, or a .glossary.json round-trip to "
        "carry the concept store. Inert for CSV / .glossary.json.",
    )
    p.set_defaults(func=_gmod_import)

    p = msub.add_parser(
        "export",
        help="Export a glossary to CSV, TBX-Basic, or .glossary.json "
        "(round-trips import; .glossary.json is the no-caveat lossless format).",
    )
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument(
        "csv",
        help="output path; .tbx writes TBX-Basic v3 (DCA), .glossary.json writes the "
        "concept-aware JSON, otherwise CSV.",
    )
    p.add_argument(
        "--dialect",
        choices=("tbx-basic", "tbx-core"),
        default="tbx-basic",
        help="TBX dialect (.tbx output): tbx-basic (default, los: private ns) or tbx-core "
        "(no los:).",
    )
    p.add_argument(
        "--strict-portable",
        dest="strict_portable",
        action="store_true",
        help="TBX only: drop the los: private namespace entirely and emit a clean "
        "pure-TBX-Basic instance (lossy; names its drops).",
    )
    p.add_argument("--force", action="store_true", help="overwrite an existing file.")
    p.set_defaults(func=_gmod_export)

    p = msub.add_parser("delete", help="Delete a glossary (DESTRUCTIVE).")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_gmod_delete)

    p = msub.add_parser("rename", help="Rename a glossary (does not revert approval).")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("name", help="the new name.")
    p.set_defaults(func=_gmod_rename)

    # --- term extraction + candidate review (feat/term-extraction) ------------
    p = msub.add_parser(
        "extract",
        help="Extract candidate terms from a document into "
        "this glossary's review queue (nothing auto-added).",
    )
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument(
        "document_id", type=bounded_id, help="document id (in the glossary's customer scope)."
    )
    p.add_argument(
        "--locale",
        "--target",
        dest="target_locale",
        default="",
        help="target locale to propose translations for.",
    )
    p.add_argument(
        "--source",
        dest="source_locale",
        default="",
        help="source locale (default: the document's project source).",
    )
    p.set_defaults(func=_gmod_extract)

    cand = msub.add_parser(
        "candidates", help="Review extracted term candidates (list, accept, drop)."
    )
    csub = cand.add_subparsers(dest="glossary_candidates_cmd", metavar="<subcommand>")
    csub.required = True

    p = csub.add_parser("list", help="List candidates awaiting review (or by --status).")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument(
        "--status",
        default="pending",
        choices=("pending", "accepted", "dropped"),
        help="which candidates to list (default: pending).",
    )
    p.set_defaults(func=_gmod_candidates_list)

    p = csub.add_parser("accept", help="Accept a candidate into the glossary as a term.")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("candidate_id", type=bounded_id, help="candidate id.")
    p.add_argument(
        "--target-term",
        default=None,
        help="override the proposed translation (else the stored proposal).",
    )
    p.set_defaults(func=_gmod_candidate_accept)

    p = csub.add_parser("drop", help="Drop a candidate (persists so it is never re-proposed).")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("candidate_id", type=bounded_id, help="candidate id.")
    p.set_defaults(func=_gmod_candidate_drop)

    # --- concept/term rich store (SPEC-terminology-backend T1): the concept level
    #     beneath the compat termbase. Every leaf delegates to the shared
    #     glossary_concepts service (web/CLI parity), scoped through _gmod_get
    #     (LAW-TERM-12 org/customer confinement). ---
    concepts = msub.add_parser(
        "concepts",
        help="Concept-level terminology (list, show, add, edit, delete).",
    )
    ccsub = concepts.add_subparsers(dest="glossary_concepts_cmd", metavar="<subcommand>")
    ccsub.required = True

    p = ccsub.add_parser("list", help="List a glossary's concepts (glossary-concept/v1).")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.set_defaults(func=_gmod_concepts_list)

    p = ccsub.add_parser("show", help="Show one concept and its terms.")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("concept_id", type=bounded_id, help="concept id.")
    p.set_defaults(func=_gmod_concept_show)

    p = ccsub.add_parser("add", help="Add a concept (optionally with terms).")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("--definition", default="", help="concept definition / gloss.")
    p.add_argument("--domain", default="", help="subject field / domain.")
    p.add_argument("--subdomain", default="", help="house sub-scope.")
    p.add_argument("--notes", default="", help="free-text notes.")
    p.add_argument(
        "--external-id",
        dest="external_id",
        default="",
        help="stable external id (TBX conceptEntry id) for round-trips.",
    )
    p.add_argument(
        "--term",
        action="append",
        default=[],
        metavar="LOCALE:SURFACE",
        help="a term as locale:surface (repeatable; e.g. en-US:invoice). A value "
        "with no colon is a wildcard-locale surface.",
    )
    p.set_defaults(func=_gmod_concept_add)

    p = ccsub.add_parser("edit", help="Edit a concept's fields (omitted flags unchanged).")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("concept_id", type=bounded_id, help="concept id.")
    p.add_argument("--definition", default=None, help="new definition.")
    p.add_argument("--domain", default=None, help="new subject field / domain.")
    p.add_argument("--subdomain", default=None, help="new sub-scope.")
    p.add_argument("--notes", default=None, help="new free-text notes.")
    p.add_argument("--external-id", dest="external_id", default=None, help="new external id.")
    p.add_argument("--source", default=None, help="new provenance / source string.")
    p.set_defaults(func=_gmod_concept_edit)

    p = ccsub.add_parser("delete", help="Delete a concept and its terms (DESTRUCTIVE).")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("concept_id", type=bounded_id, help="concept id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_gmod_concept_delete)

    terms = msub.add_parser(
        "terms",
        help="Concept terms (add, edit, delete, set-audience).",
    )
    tsub = terms.add_subparsers(dest="glossary_terms_cmd", metavar="<subcommand>")
    tsub.required = True

    p = tsub.add_parser("add", help="Add a term to a concept.")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("concept_id", type=bounded_id, help="concept id.")
    p.add_argument("--locale", default="", help="the term's locale ('' = any-locale wildcard).")
    p.add_argument("--term", required=True, help="the term surface.")
    _add_concept_term_args(p)
    p.set_defaults(func=_gmod_term_add)

    p = tsub.add_parser("edit", help="Edit a concept term (omitted flags unchanged).")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("concept_id", type=bounded_id, help="concept id.")
    p.add_argument("term_id", type=bounded_id, help="term id.")
    p.add_argument("--locale", default=None, help="new locale.")
    p.add_argument("--surface", dest="surface", default=None, help="new term surface.")
    _add_concept_term_args(p, edit=True)
    p.set_defaults(func=_gmod_term_edit)

    p = tsub.add_parser("delete", help="Delete a concept term (DESTRUCTIVE).")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument("concept_id", type=bounded_id, help="concept id.")
    p.add_argument("term_id", type=bounded_id, help="term id.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_gmod_term_delete)

    p = tsub.add_parser(
        "set-audience",
        help="Bulk-set the audience of one or more terms (human,mt,llm).",
    )
    p.add_argument("glossary_id", type=bounded_id, help="glossary id.")
    p.add_argument(
        "--term",
        dest="term_ids",
        action="append",
        type=bounded_id,
        default=[],
        required=True,
        metavar="TERM_ID",
        help="a term id to update (repeatable).",
    )
    p.add_argument(
        "--audience",
        required=True,
        help="comma-separated audience subset of human,mt,llm.",
    )
    p.set_defaults(func=_gmod_term_set_audience)
