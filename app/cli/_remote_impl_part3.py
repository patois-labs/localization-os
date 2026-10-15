"""_remote_impl: part 3 (layered pure move)

Documented in: docs/architecture/surfaces-and-parity.md
"""

import base64
import json
import os

from app.cli._common import (
    EXIT_USAGE,
    CliError,
    projected_entries_sentence,
)
from app.cli._common import apikey_expiry_cell as _apikey_expiry_cell
from app.cli._remote_impl_common import (
    _ascii_error,
    _cd_filename,
    _client,
    _parse_substitution_detail,
    _read_bytes,
    _routing_rule_line,
    _safe_download_name,
    _write_bytes,
)


def gmod_import(args, emit) -> int:
    c = _client(args)
    data = _read_bytes(args.csv)
    body = c.post(
        f"/api/glossaries/{args.glossary_id}/import",
        json={
            "csv_b64": base64.b64encode(data).decode(),
            "filename": os.path.basename(args.csv or ""),
            "source": args.source,
            "target": args.target,
            "strict": bool(getattr(args, "strict", False)),
            "concepts": bool(getattr(args, "concepts", False)),
        },
    )
    if "entries" not in body:
        # Only a concept-aware `.glossary.json` import reports concepts/terms without an
        # entries/senses key; CSV and every TBX import take the flat entry path (T2 scope
        # decision), so they report entries/senses.
        label = ".glossary.json"
        msg = (
            f"Imported {body.get('concepts', 0)} concept(s), "
            f"{body.get('terms', 0)} term(s) into glossary #{args.glossary_id} ({label})."
        )
        if "projected_entries" in body:
            # round-4 M2: the shared wording (parity with the local CLI/web/api and the three
            # descriptors) -- what THIS import's terms enforce, not a whole-glossary total.
            msg += projected_entries_sentence(body["projected_entries"])
        if body.get("dropped_refs"):
            msg += f" {body['dropped_refs']} unresolved ref(s) dropped."
        if body.get("truncated_fields"):
            msg += f" {body['truncated_fields']} field(s) truncated."
        if body.get("bindings_skipped"):
            # L-1: a bindings-bearing concept restored into a WIDER glossary is not auto-widened
            # into a configured pair its bindings never named (M-7/LT3-1); parity with the local
            # CLI/web/api wording.
            msg += (
                f" {body['bindings_skipped']} configured pair-binding(s) skipped "
                "(a bindings-bearing concept is not auto-widened)."
            )
        emit.result(msg, {"ok": True, "glossary_id": args.glossary_id, **body})
        return 0
    from .cmd_project_project import _chunks_note, _report_note

    skipped = body.get("skipped") or 0
    emit.result(
        f"Imported {body['entries']} entr{'y' if body['entries'] == 1 else 'ies'}, "
        f"{body['senses']} sense(s) into glossary #{args.glossary_id}."
        + (
            f" Skipped {skipped} entr{'y' if skipped == 1 else 'ies'} with no "
            "matching language pair."
            if skipped
            else ""
        )
        + _chunks_note(body.get("write_chunks"), body.get("concurrent_deleted"))
        + _report_note(body),
        {"ok": True, "glossary_id": args.glossary_id, **body},
    )
    return 0


def gmod_export(args, emit) -> int:
    from app.kernel import glossary as gloss_engine

    c = _client(args)
    fmt = gloss_engine.export_format_for(args.csv)
    params = {"format": fmt}
    if fmt == gloss_engine.TBX_FORMAT:
        params["dialect"] = getattr(args, "dialect", "tbx-basic")
        if getattr(args, "strict_portable", False):
            params["strict_portable"] = "1"
    body = c.get(f"/api/glossaries/{args.glossary_id}/export", params=params)
    data = base64.b64decode(body["csv_b64"])
    _write_bytes(args.csv, data, args.force)
    drops = body.get("dropped_fields") or []
    msg = f"Wrote glossary {fmt.upper()} -> {args.csv}."
    if drops:
        msg += f" Dropped {len(drops)} los: field type(s): {', '.join(drops)}."
    emit.result(
        msg,
        {
            "ok": True,
            "path": args.csv,
            "bytes": len(data),
            "format": fmt,
            "glossary_id": args.glossary_id,
            "dropped_fields": drops,
        },
    )
    return 0


def gmod_delete(args, emit) -> int:
    from ._common import confirm

    c = _client(args)
    if not confirm(emit, f"Delete glossary #{args.glossary_id}?", args.yes):
        raise CliError("aborted.", 1)
    body = c.post(f"/api/glossaries/{args.glossary_id}/delete")
    emit.result(f"Deleted glossary #{args.glossary_id}.", {"ok": True, **body})
    return 0


def gmod_rename(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/glossaries/{args.glossary_id}/rename", json={"name": args.name})
    emit.result(f"Renamed glossary #{body['id']} -> {body['name']}.", {"ok": True, **body})
    return 0


def gmod_candidates_list(args, emit) -> int:
    c = _client(args)
    body = c.get(f"/api/glossaries/{args.glossary_id}/candidates", params={"status": args.status})
    cands = body.get("candidates", [])
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "glossary_id": args.glossary_id,
                "status": body.get("status"),
                "candidates": cands,
            }
        )
    else:
        emit.table(
            [
                (
                    c_["id"],
                    c_["source_term"],
                    c_["proposed_target"] or "-",
                    f"{c_['source_locale'] or '-'}->{c_['target_locale'] or '-'}",
                    c_["frequency"],
                    c_["status"],
                )
                for c_ in cands
            ],
            headers=("id", "source", "proposed", "locales", "freq", "status"),
        )
    return 0


def gmod_extract(args, emit) -> int:
    """--remote arm of `glossary module extract` (R-MED-11): POST /api/glossaries/{gid}/
    extract. The server runs the SAME spend-capped two-stage extraction seam behind the
    route's can_tune_ai gate; a spend-cap refusal comes back as a clean 400 -> CliError.
    Was refusing with require_local though the route exists + works."""
    c = _client(args)
    body = c.post(
        f"/api/glossaries/{args.glossary_id}/extract",
        json={
            "document_id": args.document_id,
            "target": args.target_locale,
            "source": args.source_locale,
        },
        paid=True,
    )
    gid = body.get("glossary_id", args.glossary_id)
    degraded = ""
    if body.get("degraded"):
        degraded = (
            " WARNING: spend cap reached mid-run - the "
            + " and ".join(body.get("degraded_steps") or [])
            + " step was skipped, so these candidates were extracted but "
            "not fully processed (more noise - review carefully)."
        )
    emit.result(
        f"Proposed {body['proposed']} candidate(s) into glossary #{gid} "
        f"(extracted {body['extracted']}, pruned {body['pruned']}, "
        f"skipped {body['filtered_existing']} existing + "
        f"{body['filtered_history']} decided).{degraded} Review with "
        f"'glossary module candidates list {gid}'.",
        {"ok": True, **{k: v for k, v in body.items() if k != "ok"}},
    )
    return 0


def gmod_candidate_accept(args, emit) -> int:
    c = _client(args)
    body = c.post(
        f"/api/glossaries/{args.glossary_id}/candidates/{args.candidate_id}/accept",
        json={"target_term": getattr(args, "target_term", None)},
    )
    emit.result(
        f"Accepted candidate #{args.candidate_id} ('{body['source_term']}' -> "
        f"'{body['target_term']}') into glossary #{args.glossary_id}.",
        {"ok": True, **body},
    )
    return 0


def gmod_candidate_drop(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/glossaries/{args.glossary_id}/candidates/{args.candidate_id}/drop")
    emit.result(
        f"Dropped candidate #{args.candidate_id} from glossary "
        f"#{args.glossary_id} (it will never be re-proposed).",
        {"ok": True, **body},
    )
    return 0


# ============================================================================
# langset
# ============================================================================
def langset_create(args, emit) -> int:
    c = _client(args)
    body = c.post(
        "/api/langsets", json={"name": args.name, "source": args.source, "targets": args.targets}
    )
    emit.result(
        f"Created language set #{body['id']}: {body['name']} "
        f"({body['source_locale']} -> {', '.join(body['target_locales']) or 'no targets'})",
        {"ok": True, **body},
    )
    return 0


def langset_list(args, emit) -> int:
    c = _client(args)
    body = c.get("/api/langsets")
    sets = body.get("language_sets", [])
    if emit.as_json:
        emit.json({"ok": True, "language_sets": sets})
    else:
        emit.table(
            [
                (ls["id"], ls["name"], ls["source_locale"], ", ".join(ls["target_locales"]))
                for ls in sets
            ],
            headers=("id", "name", "source", "targets"),
        )
    return 0


def langset_update(args, emit) -> int:
    c = _client(args)
    body = c.post(
        f"/api/langsets/{args.langset_id}",
        json={"name": args.name, "source": args.source, "targets": args.targets or None},
    )
    emit.result(
        f"Updated language set #{body['id']}: {body['name']} "
        f"({body['source_locale']} -> {', '.join(body['target_locales']) or 'no targets'})",
        {"ok": True, **body},
    )
    return 0


def langset_delete(args, emit) -> int:
    from ._common import confirm

    c = _client(args)
    if not confirm(emit, f"Delete language set #{args.langset_id}?", args.yes):
        raise CliError("aborted.", 1)
    c.post(f"/api/langsets/{args.langset_id}/delete")
    emit.result(
        f"Deleted language set #{args.langset_id}.",
        {"ok": True, "id": args.langset_id, "deleted": True},
    )
    return 0


# ============================================================================
# doc
# ============================================================================
def doc_upload(args, emit) -> int:
    c = _client(args)
    data = _read_bytes(args.file)
    filename = os.path.basename(args.file)
    form = {"project_id": str(args.project_id), "mode": args.mode}
    # SPEC-context-metadata phase 1 / bug fix (2026-07-21): --context-json was parsed
    # into args.context_json but never forwarded here, so the API's `context` Form
    # field (app/web/api.py's api_upload_document) never saw it and the package was
    # silently dropped -- Document.context stayed NULL over --remote even though the
    # in-process CLI arm (cmd_doc._doc_upload) and the raw /api caller both stored it
    # correctly. Forward the RAW string as-is: the server validates + sanitizes via
    # the SAME context_pkg.validate_and_sanitize seam the local arm and the web
    # upload use, so a malformed package is a clean 422 (-> CliError) here too, never
    # a silent drop and never a 500. An empty/absent flag sends no `context` key at
    # all (byte-identical to today for every upload that doesn't use it).
    ctx_json = (getattr(args, "context_json", "") or "").strip()
    if ctx_json:
        form["context"] = ctx_json
    if (getattr(args, "columns", None) or "").strip():
        form["columns"] = args.columns  # Batch 6: the server parses the mapping
    body = c.upload("/api/documents", filename, data, form=form)
    if "batch_id" in body:
        # Batch 13: the server expanded a .zip into one document per supported member.
        from app.cli.cmd_doc import emit_zip_batch_result

        result = {
            k: body.get(k, [] if k in ("documents", "skipped", "failed") else "")
            for k in ("batch_id", "archive", "documents", "skipped", "failed")
        }
        return emit_zip_batch_result(emit, result, body["project_id"])
    warnings = body.get("file_warnings", [])
    if warnings and not emit.as_json:
        for w in warnings:  # every warning, as the in-process arm
            emit.say(w["message"])
    emit.result(
        f"Uploaded #{body['document_id']}: {filename} ({body.get('format')}) "
        f"into project #{body['project_id']}.",
        {
            "ok": True,
            "id": body["document_id"],
            "filename": filename,
            "fmt": body.get("format"),
            "mode": args.mode,
            "project_id": body["project_id"],
            **({"file_warnings": warnings} if warnings else {}),
        },
    )
    return 0


def doc_list(args, emit) -> int:
    c = _client(args)
    body = c.get(f"/api/projects/{args.project_id}/documents")
    docs = body.get("documents", [])
    batches = body.get("zip_batches", [])
    if emit.as_json:
        emit.json({"ok": True, "documents": docs, "zip_batches": batches})
    else:
        emit.table(
            [(d["id"], d["filename"], d["fmt"], d["mode"], d["status"]) for d in docs],
            headers=("id", "filename", "fmt", "mode", "status"),
        )
        from app.cli.cmd_doc import emit_zip_batches

        emit_zip_batches(emit, batches, args.project_id)
    return 0


def doc_show(args, emit) -> int:
    c = _client(args)
    # QA-6 (#36): --detail asks the /api for the per-segment payload (?detail=segments)
    # and renders the SAME calm summary line the local arm does, computed from the
    # API's segment_details rows -- so the remote and in-process outputs are identical
    # (the parity law: the remote arm is the /api-backed twin of the local body).
    detail = bool(getattr(args, "detail", False))
    path = f"/api/documents/{args.doc_id}/segments"
    if detail:
        path += "?detail=segments"
    info = c.get(path)
    if info.get("file_warnings") and not emit.as_json:
        for w in info["file_warnings"]:
            emit.say(w["message"])
    locales_map = info.get("locales", {})
    locale = getattr(args, "locale", None)
    if locale:
        if locale not in locales_map:
            raise CliError(f"locale not present on document: {locale}", EXIT_USAGE)
        locales = [locale]
    else:
        locales = sorted(locales_map)
    if emit.as_json:
        payload = {k: v for k, v in info.items() if k != "ok"}
        if locale:
            payload["locales"] = {locale: locales_map[locale]}
        emit.json({"ok": True, **payload})
    else:
        emit.say(
            f"Document #{info['id']}: {info['filename']} "
            f"({info['fmt']}, {info['mode']}, status={info['status']})"
        )
        emit.table(
            [
                (loc, locales_map[loc]["segments"], locales_map[loc]["translated"])
                for loc in locales
            ],
            headers=("locale", "segments", "translated"),
        )
        if info.get("a11y_images"):
            from app.filters import a11y as _a11y

            emit.say(_a11y.summary_line(info["a11y_images"]))
        for loc in locales:
            wait = locales_map[loc].get("awaiting_review")
            if wait:
                emit.say(f"  {loc}: waiting for review on run {wait['run_id']}")
        if detail:
            from app.web.api_cli import review_summary_line

            from .doc_inspect import _render_qe_details

            for loc in locales:
                line = review_summary_line(locales_map[loc].get("segment_details", []))
                if line:
                    emit.say(f"  {loc}: {line}")
                _render_qe_details(locales_map[loc].get("segment_details", []), emit)
    return 0


def doc_apply_tm(args, emit) -> int:
    """BUG-08: remote arm of `doc apply-tm`, POSTing to the new
    /api/documents/{id}/apply-tm route (mirrors the in-process arm's call to the
    same content.apply_tm_to_document(), just over HTTP)."""
    c = _client(args)
    body = c.post(f"/api/documents/{args.doc_id}/apply-tm", json={"locale": args.locale})
    applied = body.get("applied", 0)
    emit.result(
        f"Applied TM to document #{args.doc_id} ({args.locale}): "
        f"{applied} segment(s) filled from 100% TM matches.",
        {"ok": True, **body},
    )
    return 0


def doc_translate(args, emit) -> int:
    """Remote translate ENQUEUES one job per locale (the /api translate route is
    async by design). Report the job(s); the operator polls `job status --remote`."""
    c = _client(args)
    body = c.post(f"/api/documents/{args.doc_id}/translate", json={"targets": [args.locale]})
    jobs_out = body.get("jobs", [])
    emit.result(
        f"Enqueued translation of document #{args.doc_id} into {args.locale}: "
        f"{len(jobs_out)} job(s) (poll with `job status --remote`).",
        {
            "ok": True,
            "doc_id": args.doc_id,
            "locale": args.locale,
            "jobs": jobs_out,
            "skipped": body.get("skipped", []),
        },
    )
    return 0


def doc_unlock_segment(args, emit) -> int:
    """--remote arm of `doc unlock-segment`: POST /api/segments/{id}/unlock (R-MED-11).
    The server runs the SAME tm_lock.unlock_segment seam behind the route's own
    visibility + RBAC gates; a foreign/absent segment 404s -> clean CliError. Idempotent:
    `unlocked` is False when the segment carried no lock."""
    c = _client(args)
    body = c.post(f"/api/segments/{args.segment_id}/unlock", json={})
    cleared = body.get("unlocked")
    emit.result(
        (
            f"Segment #{args.segment_id}: TM lock cleared."
            if cleared
            else f"Segment #{args.segment_id}: no TM lock to clear."
        ),
        {"ok": True, "segment_id": args.segment_id, "unlocked": cleared},
    )
    return 0


def doc_approve_segment(args, emit) -> int:
    """--remote arm of `doc approve-segment`: POST /api/segments/{id}/approve. The
    server runs the shared segment_review guards; a held/deleted-key/foreign refusal
    comes back as a clean CliError via _decode."""
    c = _client(args)
    payload = {}
    if getattr(args, "acknowledge_placeholder_loss", False):
        payload["acknowledge_placeholder_loss"] = True
    if getattr(args, "no_propagate", False):
        payload["propagate"] = False
    body = c.post(f"/api/segments/{args.segment_id}/approve", json=payload)
    if body.get("tm_written"):
        tail = " Saved to TM."
    elif body.get("tm_skipped"):
        tail = " NOT saved to TM ({}).".format(body["tm_skipped"])
    else:
        tail = " Not saved to TM (not eligible)."
    from app.cli.doc_inspect import propagation_tail

    tail += propagation_tail(body)
    emit.result(f"Segment #{args.segment_id} approved.{tail}", {"ok": True, **body})
    return 0


def doc_segment_propagation(args, emit) -> int:
    """--remote arm of `doc segment-propagation`: POST /api/segments/{id}/propagation."""
    c = _client(args)
    if args.undo:
        body = c.post(f"/api/segments/{args.segment_id}/propagation", json={"undo": True})
        emit.result(
            "Segment #%d: restored %d repeat(s), kept %d."  # noqa: UP031
            % (args.segment_id, len(body.get("restored") or []), len(body.get("kept") or [])),
            {"ok": True, **body},
        )
        return 0
    body = c.post(f"/api/segments/{args.segment_id}/propagation", json={"linked": bool(args.link)})
    emit.result(
        "Segment #%d: %s%s."  # noqa: UP031
        % (
            args.segment_id,
            "linked to its repeats" if args.link else "unlinked from its repeats",
            "" if body.get("changed") else " (no change)",
        ),
        {"ok": True, **body},
    )
    return 0


def doc_export(args, emit) -> int:
    c = _client(args)
    # PARITY: `--allow-source-fallback` is a real flag on `doc export` and the /api
    # download route has accepted it since #42 (C-3) — forward it, or the flag parses,
    # looks accepted, and does NOTHING. Sent only when set, so a normal remote export's
    # query string is byte-identical to before.
    params = {"locale": args.locale}
    if getattr(args, "allow_source_fallback", False):
        params["allow_source_fallback"] = 1
    if getattr(args, "allow_held", False):
        params["allow_held"] = 1
    if getattr(args, "as_fmt", None):
        params["as_fmt"] = args.as_fmt
    resp = c.download(f"/api/documents/{args.doc_id}/download", params=params)
    data = resp.content
    # Derive a default filename like the local arm's <stem>.<locale>.<fmt>.
    out_path = args.out
    if not out_path:
        fname = _cd_filename(resp.headers.get("content-disposition", ""))
        out_path = _safe_download_name(fname, f"doc{args.doc_id}.{args.locale}.bin")
    _write_bytes(out_path, data, args.force)
    # AI Tagging 1.7 (#41): the remote arm reads resp.content as FILE BYTES, so the
    # substitution report cannot ride in the body — it comes back in the header the /api
    # download route sets. Without this the remote arm is the one surface that silently
    # ships a degraded deliverable.
    n = int(resp.headers.get("x-export-substituted") or 0)
    detail = resp.headers.get("x-export-substituted-detail") or ""
    if n:
        # "segment(s)" was a lie on the FULL_TEXT path, where these are PARAGRAPHS. Read
        # the unit out of the detail header so both arms print the same word.
        unit = "paragraph" if "unit=paragraph" in detail else "segment"
        emit.say(
            "WARNING: %d %s(s) carried broken inline-formatting tokens or placeholders; "  # noqa: UP031
            "their SOURCE text was exported instead%s."
            % (n, unit, (f" ({detail})") if detail else "")
        )
        emit.say(
            "  The file is well-formed and keeps its formatting, but those "
            f"{unit}s are UNTRANSLATED."
        )
    # R-MED-6: carry the SAME `substituted` LIST key the in-process arm emits (was only
    # `substituted_count`, an envelope mismatch). The server sends the summary detail
    # header, not the raw list, so reconstruct {unit,index,kind}; `substituted_count`
    # stays the authoritative total (a group's index list is capped server-side).
    emit.result(
        f"Exported document #{args.doc_id} ({args.locale}) -> {out_path} ({len(data)} bytes).",
        {
            "ok": True,
            "doc_id": args.doc_id,
            "locale": args.locale,
            "path": out_path,
            "bytes": len(data),
            "substituted": _parse_substitution_detail(detail),
            "substituted_count": n,
            "substituted_detail": detail,
        },
    )
    return 0


def doc_paragraphs(args, emit) -> int:
    from ._common import render_paragraph_view

    c = _client(args)
    view = _ascii_error(
        c.get, f"/api/documents/{args.doc_id}/paragraphs", params={"locale": args.locale}
    )
    render_paragraph_view(emit, args.doc_id, args.locale, view)
    if emit.as_json:
        emit.json(view)
    return 0


def doc_revert_paragraph(args, emit) -> int:
    c = _client(args)
    if args.all_damaged:
        # ATOMIC (F-3): call the batch endpoint, which pre-checks the whole batch
        # server-side and writes NOTHING on a ceiling refusal — NOT a per-index loop
        # that half-writes before the ceiling trips mid-batch.
        res = _ascii_error(
            c.post,
            f"/api/documents/{args.doc_id}/paragraphs/revert-all-damaged",
            params={"locale": args.locale},
        )
        targets = list(res.get("reverted") or res.get("indices") or [])
    else:
        if args.index is None:
            raise CliError("pass --index N (or --all-damaged).", EXIT_USAGE)
        res = _ascii_error(
            c.post,
            f"/api/documents/{args.doc_id}/paragraphs/{args.index}/revert",
            params={"locale": args.locale},
        )
        targets = [args.index]
    emit.say(
        "Reverted paragraph(s) {} of document #{} ({}) to SOURCE text.".format(
            ", ".join(str(i) for i in targets), args.doc_id, args.locale
        )
    )
    emit.say(
        "  %.0f%% of the document body is now UNTRANSLATED; the segment is "
        "back in review and will not seed TM or push to a connector."
        % ((res or {}).get("fallback_share", 0.0) * 100.0)
    )
    if emit.as_json:
        emit.json(
            {
                "ok": True,
                "doc_id": args.doc_id,
                "locale": args.locale,
                "reverted": targets,
                **(res or {}),
            }
        )
    return 0


def doc_set_paragraph(args, emit) -> int:
    c = _client(args)
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
    try:
        res = _ascii_error(
            c.post,
            f"/api/documents/{args.doc_id}/paragraphs/{args.index}/save",
            params={"locale": args.locale},
            json={"text": text, "confirm": bool(args.confirm)},
        )
    except CliError as exc:
        # The seam's tag-damage refusal is a BLOCKING CONFIRMATION, and its `message` is
        # written for the browser ("Save anyway?"). RemoteClient surfaces the server's
        # message and nothing else, so the CLI's own route forward is substituted here.
        msg = str(exc)
        if "Save anyway?" in msg:
            raise CliError(
                msg.replace("Save anyway?", "Re-run with --confirm."), exc.code
            ) from None
        raise
    emit.say(
        "Saved paragraph %d of document #%s (%s); %d damaged paragraph(s) "  # noqa: UP031
        "remain." % (args.index, args.doc_id, args.locale, res.get("damaged_count") or 0)
    )
    if emit.as_json:
        emit.json({"ok": True, "doc_id": args.doc_id, "locale": args.locale, **res})
    return 0


# ============================================================================
# job
# ============================================================================
def job_run(args, emit, payload) -> int:
    c = _client(args)
    body = c.post("/api/jobs/run", json={"kind": args.kind, "payload": payload})
    ok = body.get("ok", False)
    human = f"Job #{body.get('id')} ({body.get('kind')}) {body.get('status')}" + (
        f": {body.get('message')}" if body.get("message") else ""
    )
    emit.result(human, body)
    return 0 if ok else 1


def job_status(args, emit) -> int:
    c = _client(args)
    body = c.get(f"/api/jobs/{args.job_id}")
    d = {k: v for k, v in body.items() if k != "ok"}
    if emit.as_json:
        emit.json({"ok": True, **d})
    else:
        emit.say(
            f"Job #{d.get('id')}: {d.get('kind')} [{d.get('status')}]"
            + (f" {d.get('done')}/{d.get('total')}" if d.get("total") else "")
        )
        if d.get("message"):
            emit.say(f"  message: {d['message']}")
        if d.get("error"):
            emit.say(f"  error: {d['error']}")
        if d.get("result"):
            # #743: the finished job's result (counts, held back, failures), as in-process.
            emit.say(f"  result: {json.dumps(d['result'], default=str)}")
    return 0


def job_list(args, emit) -> int:
    c = _client(args)
    params = {"limit": args.limit, "offset": getattr(args, "offset", 0)}
    if args.kind:
        params["kind"] = args.kind
    if args.status:
        params["status"] = args.status
    if getattr(args, "actor_filter", ""):
        params["actor"] = args.actor_filter
    if getattr(args, "document_id", None) is not None:
        params["document"] = args.document_id
    if getattr(args, "workflow_id", None) is not None:
        params["workflow"] = args.workflow_id
    # Type parity with the in-process arm: --runs-only -> runs, --include-runs -> all,
    # else jobs-only (the historical default).
    if getattr(args, "runs_only", False):
        params["type"] = "run"
    elif getattr(args, "include_runs", False):
        params["type"] = "all"
    body = c.get("/api/jobs", params=params)
    rows = body.get("jobs", [])
    if emit.as_json:
        emit.json({"ok": True, "total": body.get("total"), "jobs": rows})
    else:
        emit.table(
            [
                (
                    j.get("type", "job"),
                    j["id"],
                    j["kind"],
                    j["status"],
                    (
                        f"{j.get('done', 0)}/{j.get('total', 0)}"
                        if j.get("type", "job") == "job" and j.get("total")
                        else (f"{j['duration_s']:.1f}s" if j.get("duration_s") is not None else "-")
                    ),
                    j.get("created_by") or "",
                )
                for j in rows
            ],
            headers=("type", "id", "kind/workflow", "status", "progress", "by"),
        )
    return 0


# ============================================================================
# apikey list (system-wide metadata; remote arm hits the admin-gated /api/apikeys)
# ============================================================================
def apikey_list(args, emit) -> int:
    c = _client(args)
    params = {}
    if getattr(args, "user", ""):
        params["user"] = args.user
    body = c.get("/api/apikeys", params=params)
    keys = body.get("keys", [])
    if emit.as_json:
        emit.json({"ok": True, "keys": keys})
    else:
        emit.table(
            [
                (
                    k["id"],
                    k["user"],
                    k["name"],
                    k["prefix"],
                    k["scopes"],
                    "yes" if k["active"] else "no",
                    k["last_used"] or "-",
                    _apikey_expiry_cell(k),
                )
                for k in keys
            ],
            headers=("id", "user", "name", "prefix", "scopes", "active", "last_used", "expires"),
        )
    return 0


def routing_rule_add(args, emit) -> int:
    c = _client(args)
    body = c.post(
        "/api/routing/rules",
        json={
            "ext": args.ext,
            "subject": args.subject,
            "department": args.department,
            "priority": args.priority,
            "project": args.project,
            "workflow": args.workflow,
            "language_set": args.language_set,
            "provider": args.provider,
        },
    )
    return _routing_rule_line(emit, {k: v for k, v in body.items() if k != "ok"}, "Created")


def routing_rule_list(args, emit) -> int:
    c = _client(args)
    body = c.get("/api/routing/rules")
    rules = body.get("rules", [])
    if emit.as_json:
        emit.json({"ok": True, "rules": rules})
    else:
        from app.kernel import routing

        rows = [
            (
                r["id"],
                r["priority"],
                r.get("match_ext") or "*",
                r.get("department") or "*",
                r["project_id"] if r.get("project_id") is not None else "-",
                r.get("provider") or "-",
                "yes" if r.get("active") else "no",
            )
            for r in rules
        ]
        emit.table(rows, headers=("id", "prio", "ext", "dept", "project", "provider", "active"))
        # Flag authored-but-inert reserved fields so dead config is discoverable
        # (QW-D). Byte-identical to the local `routing rule list` twin.
        inert = [
            r["id"]
            for r in rules
            if routing.reserved_fields_set(
                match_subject=r.get("match_subject") or "",
                workflow_id=r.get("workflow_id"),
                language_set_id=r.get("language_set_id"),
            )
        ]
        if inert:
            ids = ", ".join(f"#{i}" for i in inert)
            emit.say(
                f"note: rule(s) {ids} carry reserved fields (subject/workflow/"
                f"language set) that are not consulted at run time."
            )
    return 0


def routing_rule_toggle(args, emit) -> int:
    c = _client(args)
    body = c.post(f"/api/routing/rules/{args.rule_id}/toggle", json={})
    active = bool(body.get("active"))
    emit.result(
        f"Rule #{args.rule_id}: now {'active' if active else 'inactive'}.",
        {"ok": True, "id": args.rule_id, "active": active},
    )
    return 0


def routing_rule_delete(args, emit) -> int:
    c = _client(args)
    c.post(f"/api/routing/rules/{args.rule_id}/delete", json={})
    emit.result(
        f"Deleted routing rule #{args.rule_id}.", {"ok": True, "id": args.rule_id, "deleted": True}
    )
    return 0


def routing_rule_show(args, emit) -> int:
    c = _client(args)
    # A 404 (cross-org / missing, indistinguishable server-side) is mapped by the
    # RemoteClient to a clean CliError("server: not found", exit 1) — the same
    # not-found behavior every other remote read verb has (workflow show, etc.).
    body = c.get(f"/api/routing/rules/{args.rule_id}")
    d = {k: v for k, v in body.items() if k != "ok"}
    if emit.as_json:
        emit.json({"ok": True, **d})
    else:
        from app.kernel import routing

        emit.say(
            f"Rule #{d['id']} (priority {d['priority']}, {'active' if d['active'] else 'inactive'})"
        )
        emit.say(f"  match ext:     {d['match_ext'] or '*'}")
        emit.say(
            f"  match subject: {d['match_subject'] or '*'}{routing.reserved_note(d['match_subject'])}"  # noqa: E501
        )
        emit.say(f"  department:    {d['department'] or '*'}")
        emit.say(f"  project:       {d['project_id'] if d['project_id'] is not None else '-'}")
        emit.say(
            f"  workflow:      {d['workflow_id'] if d['workflow_id'] is not None else '-'}{routing.reserved_note(d['workflow_id'])}"  # noqa: E501
        )
        emit.say(
            f"  language set:  {d['language_set_id'] if d['language_set_id'] is not None else '-'}{routing.reserved_note(d['language_set_id'])}"  # noqa: E501
        )
        emit.say(f"  provider:      {d['provider'] or '-'}")
        emit.say(f"  created by:    {d['created_by'] or '-'}")
        emit.say(f"  created at:    {d['created_at'] or '-'}")
    return 0


def admin_tick(args, emit) -> int:
    # The --remote arm of `admin tick`: POST /api/admin/tick (admin scope + is_admin).
    # A 409 busy (a tick already running) maps to the RemoteClient's clean CliError.
    c = _client(args)
    body = c.post("/api/admin/tick", json={})
    n = body.get("jobs_enqueued", 0)
    emit.result(
        f"Scheduler tick ran; enqueued {n} connector-sync job(s).",
        {"ok": True, "ran": True, "jobs_enqueued": n},
    )
    return 0


# ============================================================================
# connector (operate-only over --remote: pull / push / sync). CRUD + rotate +
# test + browse stay in-process (they touch secrets / live creds server-side).
# ============================================================================
def connector_pull(args, emit) -> int:
    c = _client(args)
    # #728: a background call, polled to its end (a big pull outlived the 60 s timeout).
    body = c.post_background(f"/api/connectors/{args.connection_id}/pull")
    ok = body.get("ok", False)
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {body.get('message')}",
        {
            "ok": ok,
            "connection_id": args.connection_id,
            "message": body.get("message"),
            "document_id": body.get("document_id"),
        },
    )
    return 0 if ok else 1


def connector_push(args, emit) -> int:
    c = _client(args)
    body = c.post_background(f"/api/connectors/{args.connection_id}/push")
    ok = body.get("ok", False)
    emit.result(
        f"{'OK' if ok else 'FAILED'}: {body.get('message')}",
        {
            "ok": ok,
            "connection_id": args.connection_id,
            "message": body.get("message"),
            "pushed": body.get("pushed"),
        },
    )
    return 0 if ok else 1
