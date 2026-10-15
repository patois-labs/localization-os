"""`context ...` — the Context Package CLI (SPEC-context-metadata phase 1), the
headless half of the web context-card UI + the /api/translate/text resolution.

In-process, local-root. Business logic lives in app.services.context_pkg (the ONE
service seam web+CLI+API share) — this is a thin shell: parse args, call the service,
emit a table / block (or --json).

  context show          resolve + render the EFFECTIVE package (+ provenance) for a
                        scope (--project / --customer / --content-type / --document)
  context set-profile   create/update a standing card at a scope (--customer /
                        --content-type / --project, or none = org default), from
                        --json or individual field flags (--domain/--audience/...)
  context delete-profile  delete a card by id

Documented in: docs/architecture/context-and-adaptation.md
"""

import json

from app.kernel.context_pkg import TONE_VOCAB

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    confirm,
    op_org_id,
    refuse_over_budget,
    remote_mode,
    require_local,
    require_org_scope,
    spend_estimate,
    spend_gate,
    spend_gate_remote,
)

# #20(i): steering tone is an ENUM LIST, not free prose — words outside the curated
# vocabulary are dropped at ingestion. Document the vocabulary in --help (built from the
# one source of truth so it never drifts) instead of leaving the drop a silent surprise.
_TONE_HELP = (
    "comma-separated tone words from a curated vocabulary; words outside it "
    "are ignored (with a warning). Vocabulary: " + ", ".join(TONE_VOCAB) + "."
)


def register(subparsers) -> None:
    cx = subparsers.add_parser(
        "context", help="Context packages + standing context cards (profiles)."
    )
    sub = cx.add_subparsers(dest="context_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser(
        "show",
        help="Resolve + render the effective context package "
        "for a scope (with per-field provenance).",
    )
    p.add_argument("--project", type=bounded_id, default=None, help="project id.")
    p.add_argument("--customer", type=bounded_id, default=None, help="customer id.")
    p.add_argument("--content-type", type=bounded_id, default=None, help="content type id.")
    p.add_argument(
        "--document",
        type=bounded_id,
        default=None,
        help="document id (layers its stored context on top).",
    )
    p.set_defaults(func=_show)

    p = sub.add_parser(
        "set-profile",
        help="Create or update a standing context card "
        "at a scope (one card per exact scope). No scope flags = the "
        "org-default card.",
    )
    p.add_argument("--customer", type=bounded_id, default=None, help="scope to a customer.")
    p.add_argument(
        "--content-type",
        type=bounded_id,
        default=None,
        help="scope to a content type (with --customer = the workhorse card).",
    )
    p.add_argument("--project", type=bounded_id, default=None, help="scope to a project.")
    p.add_argument("--name", default="", help="a label for the card.")
    p.add_argument(
        "--json", dest="ctx_json", default="", help="the full context package as a JSON object."
    )
    p.add_argument("--domain", default=None)
    p.add_argument("--content-kind", default=None)
    p.add_argument("--audience", default=None)
    p.add_argument("--formality", default=None, choices=["formal", "informal", "neutral"])
    p.add_argument("--tone", default=None, help=_TONE_HELP)
    p.add_argument("--notes", default=None)
    p.add_argument("--char-limit", type=int, default=None)
    p.set_defaults(func=_set_profile)

    p = sub.add_parser("delete-profile", help="Delete a context card by id.")
    p.add_argument("profile_id", type=bounded_id)
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt.")
    p.set_defaults(func=_delete_profile)

    p = sub.add_parser(
        "set-document",
        help="Set/edit a document's context OVERRIDE "
        "(Document.context). No field flags = clear the override.",
    )
    p.add_argument("--document", type=bounded_id, required=True, help="document id.")
    p.add_argument(
        "--json", dest="ctx_json", default="", help="the full context package as a JSON object."
    )
    p.add_argument("--domain", default=None)
    p.add_argument("--content-kind", default=None)
    p.add_argument("--audience", default=None)
    p.add_argument("--formality", default=None, choices=["formal", "informal", "neutral"])
    p.add_argument("--tone", default=None, help=_TONE_HELP)
    p.add_argument("--notes", default=None)
    p.add_argument("--char-limit", type=int, default=None)
    p.add_argument("--source-title", default=None)
    p.add_argument("--source-section", default=None)
    p.add_argument("--labels", default=None, help="comma-separated source labels.")
    p.set_defaults(func=_set_document)

    p = sub.add_parser(
        "show-document",
        help="Show a document's stored context OVERRIDE "
        "plus the EFFECTIVE resolved package it sees.",
    )
    p.add_argument("--document", type=bounded_id, required=True, help="document id.")
    p.set_defaults(func=_show_document)

    p = sub.add_parser(
        "ab",
        help="A/B measure whether context improves quality: run "
        "the same segments with context ON vs OFF, score both with the "
        "QE judge, print the delta + a verdict. Numbers, not vibes.",
    )
    p.add_argument("--document", type=bounded_id, required=True, help="document id.")
    p.add_argument(
        "--locale",
        default=None,
        help="target locale (default: the doc's first available target locale).",
    )
    p.add_argument(
        "--provider",
        default=None,
        help="pin an explicit engine for a REAL "
        "measurement (default: the project's per-pair provider; a keyless "
        "install resolves to the offline mock = a structural test only).",
    )
    from app.kernel import context_ab as _ab_svc

    p.add_argument(
        "--max-segments",
        type=int,
        default=None,
        help="cap the number of segments compared (default %d, ceiling %d)."  # noqa: UP031
        % (_ab_svc.DEFAULT_MAX_SEGMENTS, _ab_svc.MAX_SEGMENTS_CEILING),
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="confirm the printed cost estimate and run (nothing is sent without it).",
    )
    p.set_defaults(func=_ab)


def _svc():
    from app.kernel import context_pkg

    return context_pkg


def _show(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.context_show(args, emit)
    require_org_scope(args, emit)
    cp = _svc()
    from app.models import Document, Project

    with _session() as s:
        document = s.get(Document, args.document) if args.document else None
        if args.document and document is None:
            raise CliError(f"no such document: {args.document}", EXIT_USAGE)
        project = s.get(Project, args.project) if args.project else None
        if args.project and project is None:
            raise CliError(f"no such project: {args.project}", EXIT_USAGE)
        # A document with no explicit project flag resolves its own project too.
        if project is None and document is not None:
            project = s.get(Project, document.project_id)
        # #515: under --org a document or project of another org is "no such ..." (the
        # sibling set-document / show-document / ab verbs already apply this guard).
        scope_org = op_org_id(args)
        if scope_org is not None:
            if document is not None:
                doc_project = s.get(Project, document.project_id)
                if doc_project is None or getattr(doc_project, "org_id", None) != scope_org:
                    raise CliError(f"no such document: {args.document}", EXIT_USAGE)
            if project is not None and getattr(project, "org_id", None) != scope_org:
                raise CliError(f"no such project: {args.project or project.id}", EXIT_USAGE)
        effective, provenance = cp.resolve(
            s,
            document=document,
            project=project,
            customer_id=args.customer,
            content_type_id=getattr(args, "content_type", None),
            org_id=op_org_id(args),
        )
        block = cp.render_block(effective)
    if emit.as_json:
        emit.json({"ok": True, "effective": effective, "provenance": provenance, "block": block})
        return 0
    if not effective:
        emit.say("No effective context for this scope.")
        return 0
    emit.say("Effective context package:")
    for field in sorted(effective):
        emit.say(f"  {field}: {effective[field]!r}  [from {provenance.get(field, '?')}]")
    emit.say("")
    emit.say("Rendered block:")
    emit.say(block or "(empty)")
    return 0


def _raw_from_args(args) -> dict:
    """Assemble the raw context dict from --json and/or individual field flags
    (individual flags OVERRIDE the JSON blob's same key)."""
    raw: dict = {}
    if (getattr(args, "ctx_json", "") or "").strip():
        try:
            obj = json.loads(args.ctx_json)
        except (ValueError, TypeError) as exc:
            raise CliError(f"--json is not valid JSON: {exc}", EXIT_USAGE) from exc
        if not isinstance(obj, dict):
            raise CliError("--json must be a JSON object.", EXIT_USAGE)
        raw.update(obj)
    for flag, key in (
        ("domain", "domain"),
        ("content_kind", "content_kind"),
        ("audience", "audience"),
        ("formality", "formality"),
        ("notes", "notes"),
    ):
        v = getattr(args, flag, None)
        if v is not None:
            raw[key] = v
    if getattr(args, "tone", None) is not None:
        raw["tone"] = [t.strip() for t in args.tone.split(",") if t.strip()]
    if getattr(args, "char_limit", None) is not None:
        raw["char_limit"] = args.char_limit
    # Optional source.* fields (set-document only; guarded so set-profile is unaffected).
    source = {
        k: getattr(args, flag, None)
        for flag, k in (("source_title", "title"), ("source_section", "section"))
        if getattr(args, flag, None) is not None
    }
    if getattr(args, "labels", None) is not None:
        source["labels"] = [x.strip() for x in args.labels.split(",") if x.strip()]
    if source:
        raw["source"] = {**(raw.get("source") or {}), **source}
    return raw


def _warn_dropped_tone(args, emit) -> None:
    """#20(i): --tone silently drops words outside TONE_VOCAB at ingestion. Surface a
    warning naming exactly which words were ignored so a headless operator isn't left
    wondering why their tone steering vanished."""
    tone = getattr(args, "tone", None)
    if tone is None:
        return
    dropped = _svc().unknown_tone_tokens(tone)
    if dropped:
        emit.warn(
            "ignored tone word(s) not in the vocabulary: "
            + ", ".join(dropped)
            + ". Allowed: "
            + ", ".join(TONE_VOCAB)
            + "."
        )


def _set_profile(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.context_set_profile(args, emit)
    require_org_scope(args, emit)
    cp = _svc()
    _warn_dropped_tone(args, emit)
    raw = _raw_from_args(args)
    with _session() as s:
        try:
            prof = cp.upsert_profile(
                s,
                org_id=op_org_id(args),
                customer_id=args.customer,
                content_type_id=getattr(args, "content_type", None),
                project_id=args.project,
                name=args.name,
                context_raw=raw,
                actor=getattr(args, "actor", None) or "cli",
            )
        except cp.ContextError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        row = cp.profile_row(prof)
    emit.result(f"Saved context card #{row['id']} [{row['scope']}].", {"ok": True, "profile": row})
    return 0


def _set_document(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.context_set_document(args, emit)
    require_org_scope(args, emit)
    cp = _svc()
    _warn_dropped_tone(args, emit)
    raw = _raw_from_args(args)
    from app.models import Document, Project

    with _session() as s:
        doc = s.get(Document, args.document)
        if doc is None:
            raise CliError(f"no such document: {args.document}", EXIT_USAGE)
        # Org guard: a foreign org's document is not editable via this scoped caller.
        org_id = op_org_id(args)
        if org_id is not None:
            proj = s.get(Project, doc.project_id)
            if proj is None or getattr(proj, "org_id", None) != org_id:
                raise CliError(f"no such document: {args.document}", EXIT_USAGE)
        try:
            stored = cp.set_document_context(s, doc, raw)
        except cp.ContextError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
    if stored:
        emit.result(
            f"Saved context override on document #{args.document}.",
            {"ok": True, "document": args.document, "context": stored},
        )
    else:
        emit.result(
            f"Cleared context override on document #{args.document}.",
            {"ok": True, "document": args.document, "context": None},
        )
    return 0


def _show_document(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    cp = _svc()
    from app.models import Document, Project

    with _session() as s:
        doc = s.get(Document, args.document)
        if doc is None:
            raise CliError(f"no such document: {args.document}", EXIT_USAGE)
        project = s.get(Project, doc.project_id)
        org_id = op_org_id(args)
        if org_id is not None and (project is None or getattr(project, "org_id", None) != org_id):
            raise CliError(f"no such document: {args.document}", EXIT_USAGE)
        stored = doc.context or {}
        effective, provenance = cp.resolve(s, document=doc, project=project, org_id=org_id)
        block = cp.render_block(effective)
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "stored": stored,
                "effective": effective,
                "provenance": provenance,
                "block": block,
            }
        )
        return 0
    emit.say(f"Document #{args.document} stored override:")
    emit.say(f"  {stored!r}" if stored else "  (none)")
    emit.say("")
    if not effective:
        emit.say("No effective context for this document.")
        return 0
    emit.say("Effective context package (what the model sees):")
    for field in sorted(effective):
        emit.say(
            f"  {field}: {effective[field]!r}  [from {cp.provenance_label(provenance.get(field))}]"
        )
    emit.say("")
    emit.say("Rendered block:")
    emit.say(block or "(empty)")
    return 0


def _ab(args, emit) -> int:
    """`context ab` — run the context A/B measurement and print the comparison.

    Thin shell over context_ab.run_ab (the ONE service seam web+CLI share). ASCII-safe
    output. Exits 0 on a completed measurement (even a Mock/structural one)."""
    if remote_mode(args):
        from app.kernel import context_ab as _svc_ab

        cap = args.max_segments if args.max_segments is not None else _svc_ab.DEFAULT_MAX_SEGMENTS
        if not spend_gate_remote(
            emit,
            args,
            verb="context ab",
            detail=f"4 calls per segment, up to {_svc_ab.clamp_max_segments(cap)} segments",
        ):
            return 0
        return _remote_impl.context_ab(args, emit)
    require_org_scope(args, emit)
    from app.kernel import context_ab
    from app.models import Document, Project

    with _session() as s:
        doc = s.get(Document, args.document)
        if doc is None:
            raise CliError(f"no such document: {args.document}", EXIT_USAGE)
        # Org guard: a foreign org's document is not measurable via this scoped caller.
        org_id = op_org_id(args)
        if org_id is not None:
            proj = s.get(Project, doc.project_id)
            if proj is None or getattr(proj, "org_id", None) != org_id:
                raise CliError(f"no such document: {args.document}", EXIT_USAGE)
        try:
            est = context_ab.estimate_ab(
                s,
                doc,
                locale=args.locale,
                provider=args.provider,
                max_segments=(
                    args.max_segments
                    if args.max_segments is not None
                    else context_ab.DEFAULT_MAX_SEGMENTS
                ),
            )
        except context_ab.ABError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        refuse_over_budget(doc.project_id, args)
        # The A/B translates every sampled segment twice and judges both: show the number and
        # require --yes BEFORE the first paid call (#531), like `eval run start`.
        if not spend_gate(
            emit,
            args,
            spend_estimate(
                est["provider"],
                est["model"],
                calls=est["calls"],
                input_tokens=est["input_tokens"],
                output_tokens=est["output_tokens"],
                note=f"{est['segments']} segment(s) x 4 calls (translate ON/OFF + QE judge each).",
            ),
            verb="context ab",
        ):
            return 0
        try:
            res = context_ab.run_ab(
                s,
                doc,
                locale=args.locale,
                provider=args.provider,
                max_segments=(
                    args.max_segments
                    if args.max_segments is not None
                    else context_ab.DEFAULT_MAX_SEGMENTS
                ),
                org_id=org_id,
            )
        except context_ab.ABError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        data = res.as_dict()
    if emit.as_json:
        emit.json({"ok": True, "ab": data})
        return 0
    emit.say(
        "Context A/B measurement -- document #{} ({})".format(data["document_id"], data["locale"])
    )
    emit.say(
        "  provider: %s%s | judge: %s | N=%d of %d translatable (cap %d)"  # noqa: UP031
        % (
            data["provider"],
            (" / " + data["model"]) if data["model"] else "",
            data["qe_provider"],
            data["n"],
            data["n_available"],
            data["max_segments"],
        )
    )
    emit.say("  mode: {}".format(data["label"]))
    emit.say("")
    if not data["context_present"]:
        emit.say("  (no context package resolved -- both arms identical)")
    on, off, delta = data["on"], data["off"], data["delta"]

    def _fmt(v, suffix=""):
        return "n/a" if v is None else (f"{v}{suffix}")

    emit.say("  Overall QE (mean, 0-100):")
    emit.say(
        "    context ON : %s  (scored %d, skipped %d)"  # noqa: UP031
        % (_fmt(on["mean_qe"]), on["scored"], on["skipped"])
    )
    emit.say(
        "    context OFF: %s  (scored %d, skipped %d)"  # noqa: UP031
        % (_fmt(off["mean_qe"]), off["scored"], off["skipped"])
    )
    emit.say("    delta      : {}".format(_fmt(delta["mean_qe"])))
    emit.say("")
    emit.say("  Terminology adherence (DNT preserved, 0-1):")
    emit.say(
        "    context ON : %s  (%d/%d preserved)"  # noqa: UP031
        % (
            _fmt(on["term_adherence"]),
            on["term_applicable"] - on["term_violations"],
            on["term_applicable"],
        )
    )
    emit.say(
        "    context OFF: %s  (%d/%d preserved)"  # noqa: UP031
        % (
            _fmt(off["term_adherence"]),
            off["term_applicable"] - off["term_violations"],
            off["term_applicable"],
        )
    )
    emit.say("    delta      : {}".format(_fmt(delta["term_adherence"])))
    emit.say("")
    emit.say("  VERDICT: {}".format(data["verdict"]))
    return 0


def _delete_profile(args, emit) -> int:
    require_local(args)
    require_org_scope(args, emit)
    cp = _svc()
    with _session() as s:
        prof = cp.get_profile(s, args.profile_id, org_id=op_org_id(args))
        if prof is None:
            raise CliError(f"no such context card: {args.profile_id}", EXIT_USAGE)
        scope = cp._scope_label(prof)
        if not confirm(
            emit, f"Delete context card #{prof.id} [{scope}]?", getattr(args, "yes", False)
        ):
            return EXIT_USAGE
        cp.delete_profile(s, args.profile_id, org_id=op_org_id(args))
    emit.result(
        f"Deleted context card #{args.profile_id}.", {"ok": True, "deleted": args.profile_id}
    )
    return 0
