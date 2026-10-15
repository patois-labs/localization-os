"""`lang list|enable|disable` — the language catalog + enabled-subset surface
(P2, item 1), the CLI half of the "Enabled languages" settings page.

In-process, local-root (same trust as editing the DB directly). Business logic
lives in app.services.languages_catalog — this is a thin shell: parse args, call
the service, emit a table (or --json). `list` reads the static BCP-47 catalog and
the enabled_locales Setting; `enable`/`disable` mutate that Setting through the
shared service (so the web page and the CLI are one source of truth — the parity
rule).

Documented in: docs/architecture/content-and-projects.md
"""

from ._common import EXIT_USAGE, CliError, require_local


def register(subparsers) -> None:
    lang = subparsers.add_parser("lang", help="Language catalog + enabled-languages registry.")
    sub = lang.add_subparsers(dest="lang_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser(
        "list", help="List languages (the enabled subset, or --all for the full catalog)."
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument(
        "--all",
        action="store_true",
        help="list the whole BCP-47 catalog (marking which are enabled).",
    )
    grp.add_argument(
        "--enabled", action="store_true", help="list only the enabled subset (the default)."
    )
    p.add_argument(
        "--search", default="", help="filter by a substring of the name / code / endonym."
    )
    p.set_defaults(func=_list)

    p = sub.add_parser(
        "enable",
        help="Enable one or more catalog codes (offered in "
        "the pickers). Additive to the current enabled set.",
    )
    p.add_argument("codes", nargs="+", help="catalog codes to enable (e.g. ja-JP hi-IN).")
    p.set_defaults(func=_enable)

    p = sub.add_parser(
        "disable",
        help="Disable one or more codes (removes them from "
        "the pickers; projects already using them are unaffected).",
    )
    p.add_argument("codes", nargs="+", help="codes to disable.")
    p.set_defaults(func=_disable)


def _matches(row: dict, needle: str) -> bool:
    if not needle:
        return True
    hay = f"{row.get('name', '')} {row.get('code', '')} {row.get('native', '')}".lower()
    return needle.lower() in hay


def _list(args, emit) -> int:
    require_local(args)
    from app.kernel import languages_catalog

    enabled = set(languages_catalog.enabled_locales())
    needle = (args.search or "").strip()
    if args.all:
        rows_src = languages_catalog.all_locales()
    else:
        # default / --enabled: the enabled subset, in enabled order.
        rows_src = languages_catalog.enabled_language_rows()
    filtered = [r for r in rows_src if _matches(r, needle)]
    # The table drops the `native` endonym column: it is non-ASCII (中文, العربية,
    # …) and a Windows cp1252 console can't encode it (every other suite prints
    # ASCII-only for this reason). The endonym stays in --json for programmatic use.
    # `dir` (a11y T5): the writing direction ("rtl" / "ltr"), the same fact
    # GET /api/languages returns as `directions` and the web pages set as `dir`.
    # Appended LAST so a script reading code / name / enabled by position is unaffected.
    rows = [
        (
            r["code"],
            r["name"],
            "yes" if r["code"] in enabled else "",
            languages_catalog.text_direction(r["code"]),
        )
        for r in filtered
    ]
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "languages": [
                    {
                        "code": r["code"],
                        "name": r["name"],
                        "native": r.get("native", ""),
                        "direction": languages_catalog.text_direction(r["code"]),
                        "enabled": r["code"] in enabled,
                    }
                    for r in filtered
                ],
                "enabled_count": len(enabled),
                "total": len(languages_catalog.all_codes()),
            }
        )
    else:
        emit.table(rows, headers=("code", "name", "enabled", "dir"))
        emit.say(
            f"  {len(enabled)} enabled of {len(languages_catalog.all_codes())} catalog languages"
        )
    return 0


def _mutate(args, emit, enable: bool) -> int:
    require_local(args)
    from app.kernel import languages_catalog

    # Validate first so a typo doesn't silently no-op (enable) or is caught (disable).
    valid, invalid = languages_catalog.validate_codes(args.codes)
    if invalid:
        raise CliError(
            "unknown catalog code(s): " + ", ".join(invalid) + " (see `lang list --all`).",
            EXIT_USAGE,
        )
    current = list(languages_catalog.enabled_locales())
    actor = getattr(args, "actor", None) or "cli"
    if enable:
        for c in valid:
            if c not in current:
                current.append(c)
    else:
        current = [c for c in current if c not in valid]
    stored = languages_catalog.set_enabled(current, actor=actor)
    verb = "enabled" if enable else "disabled"
    emit.result(
        f"{verb.capitalize()} {', '.join(valid)}. {len(stored)} language(s) now enabled.",
        {"ok": True, "changed": valid, "enabled": stored},
    )
    return 0


def _enable(args, emit) -> int:
    return _mutate(args, emit, enable=True)


def _disable(args, emit) -> int:
    return _mutate(args, emit, enable=False)
