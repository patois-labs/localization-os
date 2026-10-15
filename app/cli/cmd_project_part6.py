"""cmd_project: part6

Documented in: docs/architecture/content-and-projects.md
"""

from app import branding
from app.cli._common import (
    bounded_id,
)
from app.cli.cmd_field import register_project_verbs as _register_field_verbs
from app.cli.cmd_project_ai import (
    AI_CHOICES,
    COMMENT_CHOICES,
    _project_set_ai,
    _project_set_comments,
)
from app.cli.cmd_project_glossary import (
    _project_archive,
    _project_delete,
    _project_export,
    _project_glossaries_remove,
    _project_rename,
    _project_unarchive,
)
from app.cli.cmd_project_part2 import (
    _project_add_locale,
    _project_apply_langset,
    _project_glossaries_add,
    _project_glossaries_list,
    _project_leverage,
    _project_make_connected,
    _project_remove_locale,
    _project_set_content_type,
    _project_set_customer,
    _project_set_due,
    _project_set_intake,
    _project_set_providers,
    _project_show,
    _project_status,
    _project_tm_sources_add,
    _project_tm_sources_list,
    _project_tm_sources_remove,
)
from app.cli.cmd_project_part5 import _project_list
from app.cli.cmd_project_project import _list_window_args, _project_author, _project_create


def _register_project(subparsers) -> None:
    proj = subparsers.add_parser(
        "project", help="Projects (create, list, show, apply-langset, add-locale)."
    )
    sub = proj.add_subparsers(dest="project_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser("create", help="Create a project (+ target locales).")
    p.add_argument("name", help="project name.")
    p.add_argument("--source", default="en-US", help="source locale (default en-US).")
    p.add_argument(
        "--targets", default="", help="target locales, comma/space separated (e.g. 'fr-FR,de-DE')."
    )
    p.add_argument("--customer-id", help="customer id to attach (optional).")
    p.add_argument(
        "--provider",
        default=None,
        help="default MT provider (omit = the install's configured "
        "default_provider, same as the web new-project dialog).",
    )
    p.add_argument(
        "--seg-profile", default="", help="segmentation profile id (P6; omit = inherit chain)."
    )
    p.add_argument(
        "--content-type",
        dest="content_type",
        default="",
        help="content type id (SPEC-content-types; omit = General / the customer's default).",
    )
    p.add_argument("--due", default="", help="optional due date YYYY-MM-DD (blank = none).")
    p.add_argument(
        "--from-sdl-package",
        dest="from_sdl_package",
        default="",
        metavar="FILE",
        help="Trados project package (.sdlppx): take the source and target languages "
        "from it and add one document per target SDLXLIFF (local only).",
    )
    # Phase 2 (Intento-aligned): per-language-pair provider selection.
    p.add_argument(
        "--per-pair",
        action="store_true",
        help="PER_PAIR provider mode: each pair picks its own provider "
        "(default: UNIFORM -- --provider for every pair).",
    )
    p.add_argument(
        "--pair-provider",
        action="append",
        default=[],
        metavar="SRC:TGT=PROVIDER",
        dest="pair_provider",
        help="per-pair provider override, e.g. en-US:fr-FR=engine:2 "
        "(repeatable; implies --per-pair).",
    )
    p.add_argument(
        "--all-targets",
        action="store_true",
        dest="all_targets",
        help="suppress the warning when a target is outside the customer's "
        "active language pairs (targets are always ACCEPTED -- this "
        "only silences the confinement warning).",
    )
    # SPEC-key-based-projects 2.1. Without this the ONLY way to make a key project
    # would be the web dialog, i.e. a feature with one surface -- which the repo's
    # parity law calls an incomplete feature. Absent = "document", so every existing
    # invocation is byte-identical.
    #
    # `key_based` STAYS IN `choices` even when the kill switch is off (re-review W9,
    # accepted as-is). A-F9 asks that the switch hide the FEATURE, and it does: the
    # web type selector disappears and `_project_create` refuses with the service's
    # own message. Making these choices dynamic is not cheap -- the parser is built at
    # import time by `--help`, by the ASCII guard and by the card guard, none of which
    # may open the settings DB -- and the trade is bad: `--help` would then depend on
    # process-local DB state, and the refusal a user actually needs (an honest "the
    # key-projects switch is off") would degrade into argparse's "invalid choice".
    p.add_argument(
        "--type",
        default="",
        dest="project_type",
        choices=["", "document", "key_based"],
        help="project type (default: document). 'key_based' creates a "
        "key/value continuous-localization project and requires at "
        "least one --targets locale; refused when the "
        "key_projects_enabled switch is off.",
    )
    p.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="custom project field value (repeatable), e.g. --field jira_ticket=LOC-123 "
        "(see `field list`).",
    )
    p.add_argument(
        "--connected",
        action="store_true",
        help="create a CONNECTED project: its content comes from a connected system, not from "
        "uploads. It starts empty; connect the source with `project bind` (or on the project "
        "page). Not combinable with --type key_based or --from-sdl-package.",
    )
    p.set_defaults(func=_project_create)

    p = sub.add_parser(
        "author",
        help="Draft a project setup from a plain-language "
        "description (AI-assisted); prints the validated draft + "
        "warnings (SPEC-promptable-setup).",
    )
    p.add_argument("description", help="plain-language description of the project.")
    p.add_argument(
        "--provider",
        default="",
        help="provider for the authoring call (default: the configured "
        "default provider, same as the web dialog).",
    )
    p.add_argument(
        "--model",
        default="",
        help="authoring model id (blank = the provider's default; a keyed "
        "cloud provider resolves a default model automatically). List "
        "options with `providers models --provider <ref>`.",
    )
    p.add_argument(
        "--create",
        action="store_true",
        help="create the project from the draft (via the same "
        "content.project_create seam as `project create`).",
    )
    p.add_argument(
        "--save-template",
        dest="save_template",
        default="",
        metavar="NAME",
        help="save the draft as a project template instead of creating a project.",
    )
    p.set_defaults(func=_project_author)

    p = sub.add_parser("list", help="List projects.")
    p.add_argument(
        "--status",
        default="",
        help="filter by status (e.g. in_translation, completed, closed); "
        "'all' shows everything, blank = active (hides done/closed/cancelled).",
    )
    p.add_argument(
        "--archived",
        default="active",
        choices=("active", "all", "only"),
        help="archive filter: 'active' (default, hides archived), 'all' (both), "
        "'only' (just archived).",
    )
    p.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="KEY=EXPR",
        help="filter by a custom field (repeatable): KEY=value, KEY='~text' (contains), "
        "KEY='>=2026-01-01' for number and date fields.",
    )
    p.add_argument(
        "--connected",
        action="store_true",
        help="only connected projects (content comes from a connected system); the table adds "
        "a source column ('Not connected yet' until a source is bound).",
    )
    p.add_argument(
        "--columns",
        default="",
        help="extra columns for the table: comma separated custom field keys (e.g. jira_ticket).",
    )
    p.add_argument(
        "--sort-field",
        dest="sort_field",
        default="",
        metavar="KEY[:desc]",
        help="order by a custom field (append :desc for descending).",
    )
    _list_window_args(p)
    p.set_defaults(func=_project_list)
    _register_field_verbs(sub)

    p = sub.add_parser("show", help="Show one project (locales, docs, TM/glossary).")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_project_show)

    p = sub.add_parser(
        "leverage",
        help="7-band WORD-based TM-leverage breakdown "
        "(repetitions/context/exact/fuzzy hi-mid-lo/new).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_project_leverage)

    p = sub.add_parser(
        "apply-langset",
        help="Add every locale in a Language Set to a project (additive, idempotent).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("langset_id", help="language set id.")
    p.set_defaults(func=_project_apply_langset)

    p = sub.add_parser(
        "add-locale", help="Add one target locale to a project (additive, idempotent)."
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("locale", help="the locale to add (e.g. 'ja-JP').")
    p.add_argument(
        "--all-targets",
        action="store_true",
        dest="all_targets",
        help="suppress the warning when the locale is outside the "
        "customer's active language pairs (the locale is always "
        "ADDED -- this only silences the confinement warning).",
    )
    p.set_defaults(func=_project_add_locale)

    p = sub.add_parser(
        "remove-locale",
        help="Remove one target locale from a project "
        "(only while untranslated - a locale with translated content is "
        "permanent; deletes its seeded segments; keeps TM + glossary). On a "
        "CONNECTED project (one with a content source) the locale is only "
        "stopped at the next run: nothing is deleted and it can be added again.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("locale", help="the locale to remove (e.g. 'ja-JP').")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_project_remove_locale)

    p = sub.add_parser(
        "status",
        help="Set a project's lifecycle status "
        "(pre_translating, in_translation, completed, closed, cancelled, ...).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("status", help="the new status.")
    p.set_defaults(func=_project_status)

    p = sub.add_parser(
        "set-providers",
        help="Set a project's per-pair provider "
        "mode + mapping (Phase 2; POST /projects/{id}/pair-providers "
        "parity).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument(
        "--per-pair",
        action="store_true",
        help="PER_PAIR mode (default without this flag: UNIFORM, which "
        "also CLEARS any existing overrides).",
    )
    p.add_argument(
        "--pair-provider",
        action="append",
        default=[],
        metavar="SRC:TGT=PROVIDER",
        dest="pair_provider",
        help="per-pair provider override (repeatable; implies --per-pair).",
    )
    p.set_defaults(func=_project_set_providers)

    p = sub.add_parser(
        "set-due",
        help="Set or clear a project's optional due date "
        "(Feature A; POST /projects/{id}/due parity).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument(
        "due", nargs="?", default="", help="due date YYYY-MM-DD (omit or use --clear to clear it)."
    )
    p.add_argument("--clear", action="store_true", help="clear the due date.")
    p.set_defaults(func=_project_set_due)

    p = sub.add_parser(
        "set-content-type",
        help="Set or clear a project's content "
        "type (SPEC-content-types; POST /projects/{id}/content-type "
        "parity).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument(
        "content_type_id",
        type=bounded_id,
        nargs="?",
        default=None,
        help="content type id (omit or use --clear to clear it).",
    )
    p.add_argument("--clear", action="store_true", help="clear the content type.")
    p.set_defaults(func=_project_set_content_type)

    p = sub.add_parser(
        "set-intake",
        help="Switch a project between uploads and a connected source (only while it has no "
        "documents, no keys and no source binding; POST /api/projects/{id}/intake parity).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("intake", choices=["upload", "connected"], help="where the content comes from.")
    p.set_defaults(func=_project_set_intake)

    p = sub.add_parser(
        "make-connected",
        help="Make an existing project that a connection already feeds a connected project "
        "bound to that same connection (no schedule, no run; POST "
        "/api/projects/{id}/make-connected parity).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument(
        "--connection",
        type=bounded_id,
        default=None,
        help="connection id (needed only when more than one connection feeds the project).",
    )
    p.set_defaults(func=_project_make_connected)

    p = sub.add_parser(
        "set-customer",
        help="Assign a customer to a project that has "
        "NONE (from-no-customer only; reassignment is not supported). "
        "POST /projects/{id}/set-customer parity.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("customer_id", type=bounded_id, help="customer id to assign.")
    p.set_defaults(func=_project_set_customer)

    # --- attach an ALREADY-ESTABLISHED TM / glossary (feedback item 5) -----------
    # Web parity: the "Translation memory sources" and "Attached glossaries" cards on
    # the project page. Nested groups (like `customer pairs`) because each is a small
    # list/add/remove family over one link table. In-process only (require_local) —
    # the same posture as `project status` / `rename` / `delete`, which also write
    # tenant rows on the host DB and have no /api route.
    src = sub.add_parser(
        "tm-sources",
        help="Attach/detach ANOTHER project's "
        "translation memory as a source for this one (a reference, "
        "not a copy).",
    )
    ssub = src.add_subparsers(dest="tm_sources_cmd", metavar="<subcommand>")
    ssub.required = True

    p = ssub.add_parser("list", help="List a project's attached TM sources.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_project_tm_sources_list)

    p = ssub.add_parser(
        "add",
        help="Attach another project's TM as a source "
        "(same customer/organization only). Idempotent.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument(
        "source_project_id", type=bounded_id, help="id of the project whose TM to attach."
    )
    p.set_defaults(func=_project_tm_sources_add)

    p = ssub.add_parser(
        "remove",
        help="Detach a TM source (restores the previous matching exactly; nothing was copied).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("source_project_id", type=bounded_id, help="id of the attached source project.")
    p.set_defaults(func=_project_tm_sources_remove)

    gls = sub.add_parser(
        "glossaries",
        help="Attach/detach an EXISTING module glossary to a project (a project-level pin).",
    )
    gsub = gls.add_subparsers(dest="project_glossaries_cmd", metavar="<subcommand>")
    gsub.required = True

    p = gsub.add_parser("list", help="List a project's attached glossaries.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_project_glossaries_list)

    p = gsub.add_parser(
        "add",
        help="Attach an existing glossary to a project "
        "(a draft one contributes nothing until approved). Idempotent.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("glossary_id", type=bounded_id, help="glossary id to attach.")
    p.set_defaults(func=_project_glossaries_add)

    p = gsub.add_parser(
        "remove", help="Detach a glossary from a project (the glossary itself is untouched)."
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("glossary_id", type=bounded_id, help="attached glossary id.")
    p.set_defaults(func=_project_glossaries_remove)

    p = sub.add_parser(
        "export",
        help="Download a project's translated files "
        "(per-locale file, or a ZIP with subfolders-per-language).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument(
        "--locales",
        default="",
        help="target locales to export, comma/space separated (default: all).",
    )
    p.add_argument(
        "--all-locales",
        action="store_true",
        help="export every target locale (same as omitting --locales).",
    )
    p.add_argument("--zip", action="store_true", help="force a ZIP even for a single locale.")
    p.add_argument(
        "--batch",
        default=None,
        help="only the documents of one ZIP batch upload (the id `doc upload` printed, "
        "or `doc list`); always a ZIP laid out <locale>/<original path in the archive>.",
    )
    p.add_argument(
        "-o",
        "--out",
        help="destination path (default: "
        "<project>_translated.zip, or <stem>_<locale>.<fmt> for a single file).",
    )
    p.add_argument("--force", action="store_true", help="overwrite the file if it exists.")
    p.add_argument(
        "--allow-held",
        action="store_true",
        help="Ship a HELD locale as source anyway (chip task_9c665111). A "
        "single-file export of a locale with no committed translation is "
        "refused by default (the file would be pure source); pass this to "
        "export it explicitly. A ZIP always skip-and-reports held docs.",
    )
    p.add_argument(
        "--sdl-return-package",
        dest="sdl_return_package",
        nargs="?",
        const="*",
        default="",
        metavar="PACKAGE_GUID",
        help="build one Trados return package (.sdlrpx) from the documents imported "
        "from a Trados project package (name the package GUID when there are several).",
    )
    p.set_defaults(func=_project_export)

    p = sub.add_parser("rename", help="Rename a project.")
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("name", help="the new name.")
    p.set_defaults(func=_project_rename)

    p = sub.add_parser(
        "delete",
        help="Delete a project + ALL its owned data (documents, "
        "segments, TM, runs, ...). DESTRUCTIVE cascade; fails closed on "
        "connection/routing-rule referrers.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument(
        "--force",
        action="store_true",
        help="required to actually delete (the CLI typed-confirmation).",
    )
    p.add_argument(
        "--privacy-erasure",
        action="store_true",
        help=(
            "Also erase Lab snapshots and their results and labels, and purge the "
            f"{branding.llmemory_name()} (entries, events, suggestions) of the project."
        ),
    )
    p.set_defaults(func=_project_delete)

    p = sub.add_parser(
        "archive",
        help="Archive (retire) a project: it drops out of the "
        "default project list + pickers and is blocked from new runs; its "
        "data is preserved and it is reversible (unarchive).",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_project_archive)

    p = sub.add_parser(
        "unarchive",
        help="Unarchive (reactivate) a project: list it, offer "
        "it in pickers, and make it runnable again.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.set_defaults(func=_project_unarchive)

    p = sub.add_parser(
        "set-ai",
        help="Switch AI off or back on for a project (privacy opt-out). When AI is off: "
        "This instance's AI engines never process this project's content. "
        "Content you download, export, or connect to your own AI tools "
        "(for example the MCP agent connection) is outside this switch. MT, AI workflow steps, AI "
        "quality checks, suggestions and memory are refused; human-only workflows and TM/"
        "glossary lookups keep working. Manager/admin on the web and /api; audited.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument("state", choices=AI_CHOICES, help="off = instance AI off; on = allowed.")
    p.set_defaults(func=_project_set_ai)

    p = sub.add_parser(
        "set-comments",
        help="Translate comments and annotations in Office files (Word, Excel notes and "
        "threaded comments, PowerPoint, OpenDocument, Visio) or keep them in the source "
        "language. Authors and dates never change. Applies to files uploaded or re-imported "
        "afterwards. New projects start with it off. Manager/admin on the web and /api; audited.",
    )
    p.add_argument("project_id", type=bounded_id, help="project id.")
    p.add_argument(
        "state", choices=COMMENT_CHOICES, help="on = translate comments; off = keep them."
    )
    p.set_defaults(func=_project_set_comments)

    # Project automation: bind a content source, schedule it, run it (project_automation).
    from app.cli.cmd_project_automation import register_into as _register_automation

    _register_automation(sub)
