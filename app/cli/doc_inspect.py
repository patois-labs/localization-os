"""Document segment inspection, paragraph view/repair, and TM approval CLI verbs.

Provides subcommands for inspecting per-locale segment statuses and review
flags (`doc show`), applying 100% TM matches (`doc apply-tm`), segment-level
TM lock and review management (`doc unlock-segment`, `doc approve-segment`),
and FULL_TEXT paragraph view and repair (`doc paragraphs`, `doc revert-paragraph`,
`doc set-paragraph`).

Documented in: docs/architecture/translation-pipeline.md
"""

import functools

from . import _remote_impl
from ._common import (
    EXIT_USAGE,
    CliError,
    _actor,
    _session,
    bounded_id,
    op_org_id,
    remote_mode,
    render_paragraph_view,
    require_org_scope,
)
from .doc_common import _doc_id, _get_doc


def _register_show(sub) -> None:
    p = sub.add_parser("show", help="Show a document's segments/status per locale.")
    p.add_argument(
        "doc_id", type=functools.partial(_doc_id, subcommand="doc show"), help="document id."
    )
    p.add_argument("--locale", help="only this target locale (default: all).")
    p.add_argument(
        "--detail",
        action="store_true",
        help="include per-segment status, full QE cards and auto-fix round history (the "
        "review-quarantine "
        "detail the web editor shows); mirrors GET "
        ".../segments?detail=segments. Without it the light per-locale "
        "counts are unchanged.",
    )
    p.set_defaults(func=_doc_show)


# --- doc show ----------------------------------------------------------------
def _doc_show(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.doc_show(args, emit)
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.models import Segment

    # QA-6 (#36): --detail attaches the per-segment review payload. The serializer +
    # summary-line live in app.web.api_cli (the /api shape's owner) and are reused here
    # so the local `doc show --detail` output CANNOT drift from GET
    # .../segments?detail=segments; the --remote arm (_remote_impl.doc_show) renders the
    # SAME summary line from the API's segment_details, so all three surfaces agree. The
    # lazy import keeps a plain `doc show` (no --detail) from paying the web-layer import.
    detail = bool(getattr(args, "detail", False))
    if detail:
        from app.web.api_cli import _segment_detail_row, review_summary_line
    from app.kernel import lifecycle, orchestrator, tm_lock

    with _session() as session:
        doc = _get_doc(session, args.doc_id, op_org_id(args))
        segs = session.exec(select(Segment).where(Segment.document_id == doc.id)).all()
        by_locale: dict[str, list] = {}
        for s in segs:
            by_locale.setdefault(s.target_locale, []).append(s)
        locales = (
            sorted(by_locale)
            if not args.locale
            else ([args.locale] if args.locale in by_locale else [])
        )
        if args.locale and not locales:
            raise CliError(f"locale not present on document: {args.locale}", EXIT_USAGE)

        payload = {
            "id": doc.id,
            "filename": doc.filename,
            "fmt": doc.fmt,
            "mode": doc.mode,
            "status": lifecycle.document_status(session, doc),
            "locales": {},
        }
        detail_rows: dict[str, list] = {}
        for loc in locales:
            group = sorted(by_locale[loc], key=lambda s: s.order_index)
            translated = sum(1 for s in group if (s.target_text or "").strip())
            entry = {"segments": len(group), "translated": translated}
            # #617: same keys as GET /api/documents/{id}/segments (parity).
            waiting = orchestrator.waiting_human_review(session, doc.id, loc)
            if waiting:
                entry["awaiting_review"] = {
                    "run_id": waiting["run_id"],
                    "step": waiting["step_name"],
                }
            if detail:
                _drift = tm_lock.drifted(session, group)
                detail_rows[loc] = [
                    _segment_detail_row(s, session=session, drift=_drift) for s in group
                ]
                entry["segment_details"] = detail_rows[loc]
                if waiting and len(waiting["segments"]) == len(group):
                    for row, text in zip(detail_rows[loc], waiting["segments"], strict=True):
                        row["pending_mt"] = text
            payload["locales"][loc] = entry
        # VPAT 504.3 prompt (same helper as GET /api/documents/{id}/segments).
        from app.filters import a11y as _a11y

        _summary = _a11y.a11y_summary(doc.skeleton)
        if _summary is not None:
            payload["a11y_images"] = _summary
        warnings = (doc.skeleton or {}).get("file_warnings", [])
        if warnings:
            payload["file_warnings"] = warnings
            if not emit.as_json:
                for w in warnings:
                    emit.say(w["message"])
        if emit.as_json:
            emit.json({"ok": True, **payload})
        else:
            emit.say(
                f"Document #{doc.id}: {doc.filename} ({doc.fmt}, {doc.mode}, "
                f"status={payload['status']})"
            )
            emit.table(
                [
                    (
                        loc,
                        payload["locales"][loc]["segments"],
                        payload["locales"][loc]["translated"],
                    )
                    for loc in locales
                ],
                headers=("locale", "segments", "translated"),
            )
            if _summary is not None:
                emit.say(_a11y.summary_line(_summary))
            for loc in locales:
                wait = payload["locales"][loc].get("awaiting_review")
                if wait:
                    emit.say(f"  {loc}: waiting for review on run {wait['run_id']}")
            if detail:
                for loc in locales:
                    line = review_summary_line(detail_rows.get(loc, []))
                    if line:
                        emit.say(f"  {loc}: {line}")
                    _render_qe_details(detail_rows.get(loc, []), emit)
    return 0


def _render_qe_details(rows, emit):
    """Readable stored history, shared by local and remote document inspection."""
    import json

    drifted = sum(1 for row in rows if (row.get("lock") or {}).get("tm_changed"))
    if drifted:
        emit.say(f"  {drifted} segment(s): TM changed since fill (run tm_match again to refresh)")

    for row in rows:
        flags = [f for f in row.get("qa_flags", []) if f.get("type") in ("qe", "qe_autofix")]
        if not flags:
            continue
        emit.say(f"  Segment #{row['id']} [{row['order']}] {row['status']}:")
        for flag in flags:
            emit.say(
                "    " + str(flag.get("message", "")).encode("ascii", "backslashreplace").decode()
            )
            detail = {
                k: v for k, v in flag.items() if k not in ("type", "severity", "message", "rounds")
            }
            emit.say("    " + json.dumps(detail, ensure_ascii=True))
            for version in flag.get("rounds", []):
                emit.say(
                    f"    Round {version.get('round')}: " + json.dumps(version, ensure_ascii=True)
                )


def _register_apply_tm(sub) -> None:
    p = sub.add_parser(
        "apply-tm",
        help="Fill in target text for every segment with "
        "a 100%% TM match (web/CLI parity with POST /documents/{id}/apply-tm).",
    )
    p.add_argument(
        "doc_id", type=functools.partial(_doc_id, subcommand="doc apply-tm"), help="document id."
    )
    p.add_argument("locale", help="target locale to apply TM matches into.")
    p.set_defaults(func=_doc_apply_tm)


# --- doc apply-tm -------------------------------------------------------------
def _doc_apply_tm(args, emit) -> int:
    # BUG-08: previously in-process only (require_local unconditionally refused
    # --remote); now dispatches to POST /api/documents/{id}/apply-tm, mirroring every
    # other doc verb above.
    if remote_mode(args):
        return _remote_impl.doc_apply_tm(args, emit)
    require_org_scope(args, emit)
    from app.kernel import content, segment_review
    from app.models import Project

    with _session() as session:
        doc = _get_doc(session, args.doc_id, op_org_id(args))
        project = session.get(Project, doc.project_id)
        if project is None:
            raise CliError(f"no such project: {doc.project_id}", EXIT_USAGE)
        # Same service the web route (POST /documents/{id}/apply-tm) calls - no
        # duplicated matching/QA logic here, just the CLI shell around it.
        try:
            applied = content.apply_tm_to_document(session, doc, project, args.locale)
        except content.InvalidLocaleError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        except segment_review.AwaitingReviewError as exc:
            # same exit as a refused `doc approve-segment` (409 on web and /api)
            raise CliError(str(exc)) from exc
        emit.result(
            f"Applied TM to document #{doc.id} ({args.locale}): "
            f"{applied} segment(s) filled from 100% TM matches.",
            {"ok": True, "doc_id": doc.id, "locale": args.locale, "applied": applied},
        )
    return 0


def _register_unlock_segment(sub) -> None:
    p = sub.add_parser(
        "unlock-segment",
        help="Clear a TM lock on one segment (audited). Web/CLI parity "
        "with POST /segments/{id}/unlock.",
    )
    p.add_argument("segment_id", type=bounded_id, help="segment id to unlock.")
    p.set_defaults(func=_doc_unlock_segment)


def _doc_unlock_segment(args, emit) -> int:
    """item 5b (PROPOSAL-tm-matching §5): the CLI half of the explicit audited unlock,
    parity with the web POST /segments/{id}/unlock. --remote drives the /api route
    (R-MED-11: the /api route exists + works, so the arm is wired rather than refused;
    it inherits the route's own visibility + RBAC gates); the local arm calls the seam
    directly, org-scoped through the owning document."""
    if remote_mode(args):
        return _remote_impl.doc_unlock_segment(args, emit)
    require_org_scope(args, emit)
    from app.kernel import tm_lock
    from app.models import Segment

    with _session() as session:
        seg = session.get(Segment, args.segment_id)
        if seg is None:
            raise CliError(f"no such segment: {args.segment_id}", EXIT_USAGE)
        # scope the segment through its document (raises if out of the caller's org)
        _get_doc(session, seg.document_id, op_org_id(args))
        try:
            cleared = tm_lock.unlock_segment(session, seg, actor=(_actor(args) or "cli"))
        except ValueError as exc:  # segment_review.AwaitingReviewError (409 on web and /api)
            raise CliError(str(exc)) from exc
        emit.result(
            (
                f"Segment #{seg.id}: TM lock cleared."
                if cleared
                else f"Segment #{seg.id}: no TM lock to clear."
            ),
            {"ok": True, "segment_id": seg.id, "unlocked": cleared},
        )
    return 0


def _register_approve_segment(sub) -> None:
    p = sub.add_parser(
        "approve-segment",
        help="Approve a reviewed segment and save its pair to TM "
        "(audited). Web/CLI/API/MCP parity with POST "
        "/api/segments/{id}/approve.",
    )
    p.add_argument("segment_id", type=bounded_id, help="segment id to approve.")
    p.add_argument(
        "--acknowledge-placeholder-loss",
        action="store_true",
        help="save the pair to TM even though the target drops or adds a placeholder "
        "(by default such a segment is approved but NOT written to TM).",
    )
    p.add_argument(
        "--no-propagate",
        action="store_true",
        help="do not copy the approved target onto the document's other repeats of this "
        "source (by default every open, unlocked, unlinked repeat receives it).",
    )
    p.set_defaults(func=_doc_approve_segment)
    q = sub.add_parser(
        "segment-propagation",
        help="Unlink or relink one segment from repeat propagation, or undo its last "
        "propagation (audited). Parity with POST /api/segments/{id}/propagation.",
    )
    q.add_argument("segment_id", type=bounded_id, help="segment id.")
    grp = q.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--unlink",
        action="store_true",
        help="this segment neither receives nor sends propagated text.",
    )
    grp.add_argument("--link", action="store_true", help="propagate to and from it again.")
    grp.add_argument(
        "--undo",
        action="store_true",
        help="restore the repeats the last approval of this segment propagated "
        "(only those still exactly as propagated).",
    )
    q.set_defaults(func=_doc_segment_propagation)


def _doc_approve_segment(args, emit) -> int:
    """chip task_9c665111: approve a reviewed/edited segment and write its pair to TM,
    through the SAME segment_review seam the web CAT approve + POST /api/segments/{id}/
    approve use (identical refusal/language guards). Local arm calls the seam directly;
    --remote drives the /api route. Org-scoped through the owning document."""
    if remote_mode(args):
        return _remote_impl.doc_approve_segment(args, emit)
    require_org_scope(args, emit)
    from app.kernel import segment_review
    from app.models import Project, Segment

    with _session() as session:
        seg = session.get(Segment, args.segment_id)
        if seg is None:
            raise CliError(f"no such segment: {args.segment_id}", EXIT_USAGE)
        doc = _get_doc(session, seg.document_id, op_org_id(args))  # org-scope + existence
        project = session.get(Project, doc.project_id)
        try:
            # M6: the seam logs segment.approve (actor passed) so the CLI leaves the same
            # audit trail as web + /api.
            out = segment_review.approve_and_write_tm(
                session,
                seg,
                project,
                doc,
                actor=getattr(args, "actor", None) or "cli",
                acknowledge_placeholder_loss=bool(
                    getattr(args, "acknowledge_placeholder_loss", False)
                ),
                propagate=not getattr(args, "no_propagate", False),
            )
        except segment_review.SegmentApproveError as exc:
            raise CliError(str(exc)) from exc
        if out["tm_written"]:
            tail = " Saved to TM."
        elif out["tm_skipped"]:
            tail = " NOT saved to TM ({}).".format(out["tm_skipped"])
        else:
            tail = " Not saved to TM (not eligible)."
        tail += propagation_tail(out)
        emit.result(
            "Segment #%d approved.%s" % (seg.id, tail),  # noqa: UP031
            {"ok": True, "segment_id": seg.id, **out},  # noqa: RUF100, UP031
        )
    return 0


def propagation_tail(out) -> str:
    """The human sentence for an approve's repeat propagation (ASCII; shared by the local
    and --remote arms so both print the same words)."""
    tail = ""
    n = len(out.get("propagated") or [])
    if n:
        tail += " Applied to %d repeat%s (segments %s)." % (  # noqa: UP031
            n,
            "" if n == 1 else "s",
            ", ".join(str(i) for i in out["propagated"]),
        )
    refused = out.get("propagation_refused") or []
    if refused:
        tail += " %d repeat%s left unchanged (placeholder or tag would break: segments %s)." % (  # noqa: UP031
            len(refused),
            "" if len(refused) == 1 else "s",
            ", ".join(str(r["segment_id"]) for r in refused),
        )
    return tail


def _doc_segment_propagation(args, emit) -> int:
    """Issue #617 item 6: unlink/relink a segment from repeat propagation, or undo the last
    propagation made from it. Same seam as POST /segments/{id}/propagation and
    POST /api/segments/{id}/propagation; --remote drives the /api route."""
    if remote_mode(args):
        return _remote_impl.doc_segment_propagation(args, emit)
    require_org_scope(args, emit)
    from app.kernel import segment_review
    from app.models import Segment

    with _session() as session:
        seg = session.get(Segment, args.segment_id)
        if seg is None:
            raise CliError(f"no such segment: {args.segment_id}", EXIT_USAGE)
        doc = _get_doc(session, seg.document_id, op_org_id(args))
        actor = _actor(args) or "cli"
        if args.undo:
            try:
                res = segment_review.undo_propagation(session, seg, doc, actor=actor)
            except segment_review.AwaitingReviewError as exc:
                raise CliError(str(exc)) from exc
            emit.result(
                f"Segment #{seg.id}: restored {len(res['restored'])} repeat(s), "
                f"kept {len(res['kept'])}.",
                {"ok": True, "segment_id": seg.id, **res},
            )
            return 0
        try:
            changed = segment_review.set_propagation(
                session, seg, doc, linked=bool(args.link), actor=actor
            )
        except segment_review.AwaitingReviewError as exc:
            raise CliError(str(exc)) from exc
        emit.result(
            f"Segment #{seg.id}: "
            + ("linked to its repeats" if args.link else "unlinked from its repeats")
            + ("" if changed else " (no change)")
            + ".",
            {"ok": True, "segment_id": seg.id, "linked": bool(args.link), "changed": changed},
        )
    return 0


# --- FULL_TEXT paragraph view + recovery (issue #42) -------------------------
# All three commands are thin callers of app.services.fulltext_paragraphs, the SAME
# seam GET/POST /documents/{id}/paragraphs and the /api routes call. `tagging verify`
# is the cautionary twin: its logic is inline in cmd_tagging.py with no web twin.
#
# HUMAN RENDERING LIVES IN `_common.render_paragraph_view`, not here: the --remote arm
# renders the SAME `split_view` payload the server returns, so the two transports cannot
# print different diagnoses of one document. ASCII-safety for the runtime `⟦⟧` token
# text is `quality.ascii_reason`, the ONE formatter.
def _para_view(session, args, emit):
    """The shared load + refusal translation for the three paragraph commands."""
    from app.kernel import fulltext_paragraphs as ftp

    doc = _get_doc(session, args.doc_id, op_org_id(args))
    view = ftp.split_view(session, doc, args.locale)
    if not view.get("ok"):
        reason = view.get("reason")
        if reason == "not_full_text":
            raise CliError(
                f"document #{args.doc_id} is not mode=full_text; use the segment editor "
                "/ `doc show --detail`.",
                EXIT_USAGE,
            )
        if reason == "no_segment":
            raise CliError(
                f"no segment for document #{args.doc_id} locale {args.locale}.",
                EXIT_USAGE,
            )
        if reason == "count_desync":
            raise CliError(
                "paragraph COUNT desync: the translated body has {} paragraph(s), the "
                "source has {}. The positional map is scrambled for every later "
                "paragraph, so there is no reliable per-paragraph view and no revert is "
                "offered. Restore the blank lines between paragraphs first.".format(
                    view.get("parts"), view.get("units")
                )
            )
        raise CliError(f"cannot read paragraphs: {reason}")
    return doc, view


def _register_paragraphs(sub) -> None:
    p = sub.add_parser(
        "paragraphs",
        help="Show a FULL_TEXT document's body split back "
        "into paragraphs, with the inline-tag delta per paragraph.",
    )
    p.add_argument(
        "--doc",
        dest="doc_id",
        required=True,
        type=functools.partial(_doc_id, subcommand="doc paragraphs"),
        help="document id (must be mode=full_text).",
    )
    p.add_argument("--locale", required=True, help="target locale.")
    p.set_defaults(func=_doc_paragraphs)


def _doc_paragraphs(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.doc_paragraphs(args, emit)
    require_org_scope(args, emit)
    with _session() as session:
        doc, view = _para_view(session, args, emit)
        render_paragraph_view(emit, doc.id, args.locale, view)
        # The human lines above are already suppressed under --json; emit the payload
        # directly rather than emit.result("", ...) so human mode does not get a stray
        # blank line.
        if emit.as_json:
            emit.json(view)
    return 0


def _register_revert_paragraph(sub) -> None:
    p = sub.add_parser(
        "revert-paragraph",
        help="Replace one FULL_TEXT paragraph with its SOURCE text to "
        "recover from broken inline tags (audited; the paragraph "
        "ships UNTRANSLATED and the segment goes to review).",
    )
    p.add_argument(
        "--doc",
        dest="doc_id",
        required=True,
        type=functools.partial(_doc_id, subcommand="doc revert-paragraph"),
        help="document id (must be mode=full_text).",
    )
    p.add_argument("--locale", required=True, help="target locale.")
    p.add_argument("--index", type=int, help="paragraph index (0-based).")
    p.add_argument(
        "--all-damaged",
        action="store_true",
        help="revert EVERY damaged paragraph instead of one --index "
        "(atomic: refused as a whole above the share ceiling).",
    )
    p.set_defaults(func=_doc_revert_paragraph)


def _doc_revert_paragraph(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.doc_revert_paragraph(args, emit)
    require_org_scope(args, emit)
    from app.kernel import fulltext_paragraphs as ftp

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        doc, _view = _para_view(session, args, emit)
        if args.all_damaged:
            # ATOMIC batch (F-3): revert_all_damaged pre-checks the WHOLE batch against
            # the share ceiling and writes NOTHING on a refusal, instead of looping
            # revert_paragraph and half-writing before the ceiling trips mid-batch.
            res = ftp.revert_all_damaged(session, doc, args.locale, actor=actor)
            if not res.get("ok"):
                raise CliError(
                    "revert-all-damaged refused: %s" % (res.get("message") or res.get("reason")),
                    EXIT_USAGE,
                )
            targets = list(res.get("reverted") or res.get("indices") or [])
        else:
            if args.index is None:
                raise CliError("pass --index N (or --all-damaged).", EXIT_USAGE)
            res = ftp.revert_paragraph(session, doc, args.locale, args.index, actor=actor)
            if not res.get("ok"):
                raise CliError(
                    "paragraph %d not reverted: %s"  # noqa: UP031
                    % (args.index, res.get("message") or res.get("reason")),
                    EXIT_USAGE,
                )
            targets = [args.index]
        emit.say(
            "Reverted paragraph(s) {} of document #{} ({}) to SOURCE text.".format(
                ", ".join(str(i) for i in targets), doc.id, args.locale
            )
        )
        emit.say(
            "  %.0f%% of the document body is now UNTRANSLATED; the segment is "
            "back in review and will not seed TM or push to a connector."
            % (res["fallback_share"] * 100.0)
        )
        if emit.as_json:
            emit.json(
                {"ok": True, "doc_id": doc.id, "locale": args.locale, "reverted": targets, **res}
            )
    return 0


def _register_set_paragraph(sub) -> None:
    p = sub.add_parser(
        "set-paragraph",
        help="Replace one FULL_TEXT paragraph's translation (the hand "
        "repair: retype the inline tags around the words).",
    )
    p.add_argument(
        "--doc",
        dest="doc_id",
        required=True,
        type=functools.partial(_doc_id, subcommand="doc set-paragraph"),
        help="document id (must be mode=full_text).",
    )
    p.add_argument("--locale", required=True, help="target locale.")
    p.add_argument("--index", type=int, required=True, help="paragraph index (0-based).")
    p.add_argument("--text", help="the new paragraph text (inline tags included).")
    p.add_argument(
        "--from-file",
        dest="from_file",
        help="read the new paragraph text from this UTF-8 file instead.",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="save even when the result breaks inline-tag integrity "
        "(refused without this, exactly like the editor's save guard).",
    )
    p.set_defaults(func=_doc_set_paragraph)


def _doc_set_paragraph(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.doc_set_paragraph(args, emit)
    require_org_scope(args, emit)
    from app.kernel import fulltext_paragraphs as ftp

    actor = getattr(args, "actor", None) or "cli"
    if (args.text is None) == (not args.from_file):
        raise CliError("pass exactly one of --text or --from-file.", EXIT_USAGE)
    if args.from_file:
        try:
            with open(args.from_file, encoding="utf-8") as fh:
                text = fh.read().rstrip("\n")
        except OSError as exc:
            raise CliError(f"cannot read {args.from_file}: {exc}", EXIT_USAGE) from exc
    else:
        text = args.text
    with _session() as session:
        doc, _view = _para_view(session, args, emit)
        res = ftp.save_paragraph(
            session, doc, args.locale, args.index, text, actor=actor, confirm=bool(args.confirm)
        )
        if not res.get("ok"):
            # The SEAM classifies the event; this surface only chooses how to say it.
            if ftp.outcome_class(res) == "confirmable":
                # `detail` is a RUNTIME reason string embedding real U+27E6/U+27E7
                # tokens, so it must go through the ONE formatter. `_write_ascii_safe`
                # stops the cp1252 crash but renders `⟦r1⟧` as a backslash escape exactly
                # when the operator most needs to read it.
                from app.kernel import quality as _q

                raise CliError(
                    "refused: this text breaks inline-tag integrity ({}) - formatting "
                    "would be lost on export. Fix the tokens, or re-run with --confirm.".format(
                        _q.ascii_reason(res.get("detail"))
                    )
                )
            raise CliError(
                "paragraph not saved: %s" % (res.get("message") or res.get("reason")), EXIT_USAGE
            )
        emit.say(
            "Saved paragraph %d of document #%s (%s); %d damaged paragraph(s) "  # noqa: UP031
            "remain." % (args.index, doc.id, args.locale, res["damaged_count"])
        )
        if emit.as_json:
            emit.json({"ok": True, "doc_id": doc.id, "locale": args.locale, **res})
    return 0


def register_doc_inspect_commands(sub) -> None:
    """Register all document inspection, TM, and paragraph subcommands."""
    _register_show(sub)
    _register_paragraphs(sub)
    _register_revert_paragraph(sub)
    _register_set_paragraph(sub)
    _register_apply_tm(sub)
    _register_unlock_segment(sub)
    _register_approve_segment(sub)


__all__ = [
    "_doc_apply_tm",
    "_doc_approve_segment",
    "_doc_paragraphs",
    "_doc_revert_paragraph",
    "_doc_set_paragraph",
    "_doc_show",
    "_doc_unlock_segment",
    "_para_view",
    "_register_apply_tm",
    "_register_approve_segment",
    "_register_paragraphs",
    "_register_revert_paragraph",
    "_register_set_paragraph",
    "_register_show",
    "_register_unlock_segment",
    "register_doc_inspect_commands",
]
