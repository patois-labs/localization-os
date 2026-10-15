"""`ask` command: the grounded module assistant on the CLI (SPEC-ask-draft section 4).

    loc-tms ask "how do I teach it a terminology rule?"
    loc-tms ask memories "how do I teach it a terminology rule?"

Answers come ONLY from the repo-tracked capability cards in `app/capabilities/`
(app.services.assist owns that contract). Naming a module grounds the answer on that
module's card plus every card it connects to; omitting it grounds on the whole card
set -- the overview.

LOCAL-ONLY BY DESIGN. The spec's non-goals rule out an /api surface for Ask+Draft
(consistent with the workflow-authoring deferral), so `--remote` is refused LOUDLY
(require_local, exit 2) rather than silently answering from the local host -- the
DATA-SAFETY posture every local-only verb in this CLI follows.

ASCII-safe output (no em-dashes/emoji/box chars -- Windows consoles).

Documented in: docs/architecture/assistant.md
"""

from ._common import EXIT_USAGE, CliError, require_local


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "ask",
        help="Ask a plain-language question about a module, answered ONLY from "
        "that module's shipped guide (never invented).",
    )
    p.add_argument(
        "module",
        nargs="?",
        default="",
        help="module slug (connectors|documents|engines|"
        "glossary|images|keys|llm|reports|routing|tm|"
        "vendors|workflows; feature-gated ones resolve only while "
        "their feature is on). Omit to ask across the guides "
        "-- every one except the reference documents flagged "
        "overview_exclude (today just `llm`, which is reachable by "
        "naming it).",
    )
    p.add_argument("question", help="the question, in quotes.")
    p.set_defaults(func=_ask)


def _ask(args, emit) -> int:
    require_local(args)
    from app.kernel import assist

    module = (args.module or "").strip().lower()
    question = (args.question or "").strip()
    if not assist.enabled():
        raise CliError(
            "Ask and Draft are switched off for this install (config set assist_enabled 1).",
            EXIT_USAGE,
        )
    if module and assist.load_card(module) is None:
        raise CliError(
            "no guide for {!r}. Available: {}".format(module, ", ".join(assist.card_slugs())),
            EXIT_USAGE,
        )
    try:
        result = assist.ask(module, question)
    except assist.AssistDisabled as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    except assist.AssistError as exc:
        raise CliError(exc.message) from exc

    if emit.as_json:
        emit.json({"ok": True, **result})
        return 0
    emit.say(result["answer"])
    emit.say("")
    emit.say("-- {}".format(result["grounding"]))
    if result["engine_note"]:
        emit.warn(result["engine_note"])
    # With no module the answer is grounded on the WHOLE card set, so the pointer is
    # the index. It used to print the literal "/help/<module>", which is not a page.
    if module:
        emit.say(f"Full guide: /help/{module}")
    else:
        emit.say("Full guides: /help")
    return 0
