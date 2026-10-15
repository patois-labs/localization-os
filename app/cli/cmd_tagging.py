"""`tagging` command group — batch inline-tag integrity re-verification.

AI Tagging risk #4 (existing polluted targets): segments already damaged by past
fix steps are not re-checked until re-translated or re-saved. This is the one-shot
forward-and-backward sweep: it runs the SAME deterministic verifier the write gate
uses (quality.tag_damage) over existing stored targets and FLAGS the damaged ones
for review. It NEVER auto-repairs or rewrites text (the reviewer decides); the web
surface for the results is the existing needs-review queue / editor chips.

  tagging verify [--project ID] [--doc ID] [--locale L] [--apply]

By DEFAULT it is a DRY RUN — it reports the damaged segments and changes nothing
(safe on the real DB). With `--apply` it persists, for each damaged segment, a
tag_integrity qa_flag and routes it to NEEDS_REVIEW (mirroring the write gate), so
the linguist sees exactly the segments a live commit would have caught. In-process
only (a local-root maintenance sweep); --remote is not supported.

Documented in: docs/architecture/inline-tags.md
"""

from ._common import (
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    op_org_id,
    refuse_over_budget,
    remote_mode,
    require_org_scope,
    spend_estimate,
    spend_gate,
)


def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "tagging", help="Inline-formatting tag integrity (batch re-verify)."
    )
    sub = grp.add_subparsers(dest="tagging_cmd", metavar="<subcommand>")
    sub.required = True

    p = sub.add_parser(
        "verify",
        help="Scan stored targets for broken inline-formatting "
        "tokens; flag damaged segments for review (dry-run by default).",
    )
    p.add_argument(
        "--project",
        dest="project_id",
        type=bounded_id,
        default=None,
        help="limit to one project id.",
    )
    p.add_argument(
        "--doc", dest="doc_id", type=bounded_id, default=None, help="limit to one document id."
    )
    p.add_argument("--locale", default="", help="limit to one target locale.")
    p.add_argument(
        "--apply",
        action="store_true",
        help="persist a tag_integrity qa_flag + NEEDS_REVIEW on each damaged "
        "segment (default: dry-run, report only, no writes).",
    )
    p.set_defaults(func=_verify)

    r = sub.add_parser(
        "repair",
        help="Try an AI repair of broken inline-formatting "
        "tokens on damaged segments (dry-run by default).",
    )
    r.add_argument(
        "--project",
        dest="project_id",
        type=bounded_id,
        default=None,
        help="limit to one project id.",
    )
    r.add_argument(
        "--doc", dest="doc_id", type=bounded_id, default=None, help="limit to one document id."
    )
    r.add_argument(
        "--segment", dest="segment_id", type=bounded_id, default=None, help="repair one segment id."
    )
    r.add_argument("--locale", default="", help="limit to one target locale.")
    r.add_argument(
        "--limit",
        type=int,
        default=200,
        help="max segments to repair in one sweep (default 200) -- bounds "
        "provider spend on a wide run.",
    )
    r.add_argument(
        "--apply",
        action="store_true",
        help="persist the repaired text (default: dry-run -- the provider "
        "calls still run so the report is real, but nothing is written).",
    )
    r.add_argument(
        "--yes",
        action="store_true",
        help="confirm the printed cost estimate and make the provider calls. Without it the "
        "command only previews: it lists the damaged segments and the estimate, and calls "
        "no provider.",
    )
    r.set_defaults(func=_repair)


def _repair(args, emit) -> int:
    """AI Tagging 1.5c on-demand: hand already-damaged segments to the LLM repair rung
    (explicit per-token brief -> repair call -> structural + body-preservation locks ->
    independent placement verification) and report/persist what it could certify.

    Thin caller: ALL logic lives in `tag_llm_repair.repair_stored_segments`, the same
    seam the web editor's repair action uses. In-process only, like `verify` (a
    local-root maintenance sweep that spends provider budget)."""
    if remote_mode(args):
        raise CliError("tagging repair is in-process only (no --remote).", EXIT_USAGE)
    require_org_scope(args, emit)
    from app.kernel import tag_llm_repair

    if not tag_llm_repair.enabled():
        raise CliError(
            "tag_llm_repair_enabled is off; turn it on to use AI tag repair.", EXIT_USAGE
        )
    if args.limit < 1:
        raise CliError("--limit must be 1 or more.", EXIT_USAGE)
    scope = {
        "org_id": op_org_id(args),
        "project_id": args.project_id,
        "doc_id": args.doc_id,
        "locale": args.locale,
        "segment_id": args.segment_id,
        "limit": args.limit,
    }
    with _session() as session:
        # The sweep spends on the provider even as a dry run (#531), so first PLAN it for free,
        # price it, and only call the provider once the operator confirmed with --yes.
        plan_rep = tag_llm_repair.repair_stored_segments(session, plan_only=True, **scope)
        plan = plan_rep["plan"]
        if plan["providers"]:
            for pid in plan["projects"]:
                refuse_over_budget(pid, args)
            parts = [
                spend_estimate(
                    name,
                    v["model"],
                    calls=v["calls"],
                    input_tokens=v["in"],
                    output_tokens=v["out"],
                )
                for name, v in sorted(plan["providers"].items())
            ]
            est = {
                "provider": ", ".join(p["provider"] for p in parts),
                "model": "",
                "calls": sum(p["calls"] for p in parts),
                "input_tokens": sum(p["input_tokens"] for p in parts),
                "output_tokens": sum(p["output_tokens"] for p in parts),
                "llm_usd": round(sum(p["llm_usd"] for p in parts), 6),
                "jev_usd": 0.0,
                "total_usd": round(sum(p["total_usd"] for p in parts), 6),
                "free": all(p["free"] for p in parts),
                "note": f"{plan_rep['attempted']} damaged segment(s) planned"
                + (" (stopped at --limit)." if plan_rep["truncated"] else "."),
            }
            if not spend_gate(emit, args, est, verb="tagging repair"):
                if not emit.as_json:
                    emit.table(
                        [
                            (s["segment_id"], s["document_id"], s["locale"], s["order"], s["was"])
                            for s in plan_rep["segments"]
                        ],
                        headers=("segment", "document", "locale", "order", "damage"),
                    )
                return 0
        rep = tag_llm_repair.repair_stored_segments(session, apply=args.apply, **scope)

    if emit.as_json:
        emit.json({"ok": True, **rep})
        return 0
    if not rep["segments"]:
        emit.say("No damaged inline tags found: nothing to repair.")
        return 0
    emit.table(
        [
            (
                s["segment_id"],
                s["document_id"],
                s["locale"],
                s["order"],
                "yes" if s["repaired"] else "no",
                "yes" if s["verified"] else "no",
                s["note"] or s["was"],
            )
            for s in rep["segments"]
        ],
        headers=("segment", "document", "locale", "order", "repaired", "verified", "note"),
    )
    verb = "repaired" if rep["applied"] else "would be repaired (dry-run -- pass --apply)"
    emit.say(
        f"{rep['repaired']} of {rep['scanned']} damaged segment(s) {verb}; "
        f"{rep['verified']} placement-verified. Spend: ${rep['cost_usd']:.4f}."
    )
    if rep["truncated"]:
        emit.say(
            f"NOTE: stopped at the --limit of {rep['limit']}; more damaged "
            "segments remain. Re-run to continue."
        )
    return 0


def _verify(args, emit) -> int:
    if remote_mode(args):
        raise CliError("tagging verify is in-process only (no --remote).", EXIT_USAGE)
    require_org_scope(args, emit)
    from sqlmodel import select

    from app.kernel import quality
    from app.models import Document, Project, Segment, SegStatus

    org_id = op_org_id(args)
    damaged: list[dict] = []
    scanned = 0
    with _session() as session:
        stmt = select(Segment, Document, Project).where(
            Segment.document_id == Document.id, Document.project_id == Project.id
        )
        if org_id is not None:
            stmt = stmt.where(Project.org_id == org_id)
        if args.project_id is not None:
            stmt = stmt.where(Project.id == args.project_id)
        if args.doc_id is not None:
            stmt = stmt.where(Document.id == args.doc_id)
        if args.locale:
            stmt = stmt.where(Segment.target_locale == args.locale)
        for seg, doc, _proj in session.exec(stmt).all():
            if not (seg.target_text or "").strip():
                continue
            scanned += 1
            reason = quality.tag_damage(seg.source_text, seg.target_text)
            if not reason:
                continue
            damaged.append(
                {
                    "segment_id": seg.id,
                    "document_id": doc.id,
                    "locale": seg.target_locale,
                    "order": seg.order_index,
                    "reason": reason,
                }
            )
            if args.apply:
                seg.qa_flags = quality.merge_tag_flags(seg.qa_flags, reason)
                seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
        if args.apply and damaged:
            session.commit()

    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "scanned": scanned,
                "damaged": len(damaged),
                "applied": bool(args.apply),
                "segments": damaged,
            }
        )
        return 0
    if not damaged:
        emit.say(f"Scanned {scanned} translated segment(s): no inline-tag damage found.")
        return 0
    emit.table(
        [
            (d["segment_id"], d["document_id"], d["locale"], d["order"], d["reason"])
            for d in damaged
        ],
        headers=("segment", "document", "locale", "order", "reason"),
    )
    verb = (
        "flagged for review"
        if args.apply
        else "would be flagged (dry-run -- pass --apply to persist)"
    )
    emit.say(f"{len(damaged)} of {scanned} translated segment(s) have broken inline tags; {verb}.")
    return 0
