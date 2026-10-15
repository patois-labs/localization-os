"""content: part2

Documented in: docs/architecture/content-and-projects.md
"""

from sqlmodel import Session, select

from app.models import (
    Document,
    Project,
    ProjectPairProvider,
    Segment,
    TargetLocale,
)
from app.services import languages, languages_catalog
from app.services.content_project_rename_delete_sp import (
    _CD_UNSAFE_RE,
    _CONTROL_CHARS_RE,
    InvalidLocaleError,
    _locale_query_thread_ids,
    _materialize_key_values,
    _project_job_ids,
    _project_locale_norms,
    _project_locale_rows,
    _seed_locale_segments,
    header_safe_filename,
    parse_due_date,
    translated_target_locales,
)


def set_project_due(
    session: Session, project_id: int, due_date, *, actor: str = "system", ip: str = "", org_id=None
) -> Project | None:
    """Set or clear a project's due date (Feature A). `due_date` may be a
    YYYY-MM-DD string, a datetime, or None/"" to CLEAR it. Validates the input
    (raises DueDateError on garbage) BEFORE writing. Audits `project.due_set`
    (a value set) or `project.due_cleared` (cleared to None). Reuses the org guard
    the other project mutations use: an `org_id` that doesn't match the project's
    resolves to "not found" (None), exactly like _get_project in the CLI. Returns
    the Project, or None when it doesn't exist / is cross-org."""
    project = session.get(Project, project_id)
    if project is None:
        return None
    if org_id is not None and getattr(project, "org_id", None) != org_id:
        return None
    parsed = parse_due_date(due_date)  # raises DueDateError before any write
    project.due_date = parsed
    session.add(project)
    session.commit()
    session.refresh(project)
    from app.services import audit

    if parsed is None:
        audit.log(
            actor=actor or "system",
            action="project.due_cleared",
            target=f"project {project.id}",
            ip=ip,
            customer_id=project.customer_id,
        )
    else:
        audit.log(
            actor=actor or "system",
            action="project.due_set",
            target=f"project {project.id}",
            detail=parsed.strftime("%Y-%m-%d"),
            ip=ip,
            customer_id=project.customer_id,
        )
    return project


def delete_project_preview(session: Session, project_id: int) -> dict | None:
    """Itemized, read-only counts of what a project delete would destroy (the OWNED
    cascade) and what currently BLOCKS it (independent referrers). Shown BEFORE the
    typed confirm on every surface. `blockers.total` > 0 means the delete fails closed.
    Returns None if the project doesn't exist. Never mutates."""
    from app.models import (
        AdvancedMission,
        AdvancedRun,
        AdvancedStagedTarget,
        AdvancedTranscriptEntry,
        Connection,
        ContextProfile,
        DeliveryEmptyApproval,
        Document,
        FullTextTM,
        GlossaryEntry,
        KeyChangeLog,
        KeyImage,
        LQAResult,
        MemoryEvent,
        MemorySuggestion,
        ProjectBudget,
        ProjectGlossary,
        ProjectMember,
        ProjectPairProvider,
        ProjectTmSource,
        QualityFinding,
        QueryPost,
        QueryThread,
        RoutingRule,
        Segment,
        SegmentEmbedding,
        SegmentPostEdit,
        SegmentTM,
        StringKey,
        TargetLocale,
        VendorDispatch,
        Workflow,
        WorkflowRun,
    )

    project = session.get(Project, project_id)
    if project is None:
        return None

    def _n(stmt) -> int:
        return len(session.exec(stmt).all())

    doc_ids = list(session.exec(select(Document.id).where(Document.project_id == project_id)).all())
    seg_n = _n(select(Segment.id).where(Segment.document_id.in_(doc_ids))) if doc_ids else 0
    run_n = _n(select(WorkflowRun.id).where(WorkflowRun.document_id.in_(doc_ids))) if doc_ids else 0
    lqa_n = _n(select(LQAResult.id).where(LQAResult.document_id.in_(doc_ids))) if doc_ids else 0
    # Post-edit episode rows are a Segment child this delete removes (§3.2.6) — count
    # them so the confirm dialog does not understate what is destroyed. Denormalized
    # document_id makes this a doc-scoped count without a segment join.
    pe_n = (
        _n(select(SegmentPostEdit.id).where(SegmentPostEdit.document_id.in_(doc_ids)))
        if doc_ids
        else 0
    )
    # Query threads + posts are project children (SPEC-wave3-collaboration §3.5) this
    # delete removes — counted so the confirm dialog does not understate the destruction.
    # Threads are project-scoped, so count by project_id directly.
    q_thread_ids = list(
        session.exec(select(QueryThread.id).where(QueryThread.project_id == project_id)).all()
    )
    q_thread_n = len(q_thread_ids)
    q_post_n = (
        _n(select(QueryPost.id).where(QueryPost.thread_id.in_(q_thread_ids))) if q_thread_ids else 0
    )
    # Runs of this project (its documents' runs OR a project-scoped workflow's runs) —
    # the anchor the VendorDispatch + MemoryEvent cascade keys off (#63). Mirrors the run
    # id gather in delete_project so the preview counts exactly what the delete removes.
    wf_ids = list(session.exec(select(Workflow.id).where(Workflow.project_id == project_id)).all())
    run_ids: set = set()
    if doc_ids:
        run_ids.update(
            session.exec(select(WorkflowRun.id).where(WorkflowRun.document_id.in_(doc_ids))).all()
        )
    if wf_ids:
        run_ids.update(
            session.exec(select(WorkflowRun.id).where(WorkflowRun.workflow_id.in_(wf_ids))).all()
        )
    run_ids = list(run_ids)
    # VendorDispatch legs (deleted, #63) and MemoryEvent episodes (un-scoped, #63) this
    # delete touches — added so the preview stops claiming a project that used Vendor
    # Studio / LLMemory is a clean no-cascade delete. A leg is anchored to a run; a memory
    # event is reachable via project_id, this project's documents, or its runs.
    vd_n = (
        _n(select(VendorDispatch.id).where(VendorDispatch.workflow_run_id.in_(run_ids)))
        if run_ids
        else 0
    )
    from sqlalchemy import or_ as _pv_or

    _memev_conds = [MemoryEvent.project_id == project_id]
    if doc_ids:
        _memev_conds.append(MemoryEvent.document_id.in_(doc_ids))
    if run_ids:
        _memev_conds.append(MemoryEvent.workflow_run_id.in_(run_ids))
    memev_n = _n(select(MemoryEvent.id).where(_pv_or(*_memev_conds)))
    # ADVANCED WORKFLOWS (SPEC-advanced-workflows §2): missions + runs carry a hard FK to
    # project.id, and a run's own children (findings on EITHER leg, staged targets, transcript
    # rows) go with it. Counted for EVERY project -- 0 on any project that never ran a mission, so
    # the preview shape is unchanged for them -- so the confirm dialog stops calling an
    # Advanced-tier project a clean no-cascade delete. Keys AND scope mirror
    # advanced.delete_project_runs exactly, so what the preview promises is what step 8f reports.
    adv_run_ids = list(
        session.exec(select(AdvancedRun.id).where(AdvancedRun.project_id == project_id)).all()
    )
    adv_staged_ids = (
        list(
            session.exec(
                select(AdvancedStagedTarget.id).where(AdvancedStagedTarget.run_id.in_(adv_run_ids))
            ).all()
        )
        if adv_run_ids
        else []
    )
    adv_te_n = (
        _n(
            select(AdvancedTranscriptEntry.id).where(
                AdvancedTranscriptEntry.run_id.in_(adv_run_ids)
            )
        )
        if adv_run_ids
        else 0
    )
    adv_qf_n = (
        _n(
            select(QualityFinding.id).where(
                _pv_or(
                    QualityFinding.run_id.in_(adv_run_ids),
                    QualityFinding.staged_target_id.in_(adv_staged_ids or [-1]),
                )
            )
        )
        if adv_run_ids
        else 0
    )
    cascade = {
        "documents": len(doc_ids),
        "segments": seg_n,
        "target_locales": _n(select(TargetLocale.id).where(TargetLocale.project_id == project_id)),
        "pair_providers": _n(
            select(ProjectPairProvider.id).where(ProjectPairProvider.project_id == project_id)
        ),
        "tm_segments": _n(select(SegmentTM.id).where(SegmentTM.project_id == project_id)),
        "tm_fulltext": _n(select(FullTextTM.id).where(FullTextTM.project_id == project_id)),
        "embeddings": _n(
            select(SegmentEmbedding.id).where(SegmentEmbedding.project_id == project_id)
        ),
        "workflow_runs": run_n,
        # #63: legs this delete REMOVES (their anchor run is destroyed) and memory
        # episodes it UN-SCOPES (project/document/segment/run FKs -> NULL; the customer
        # keeps what it learned). 0 on any project that never used Vendor Studio / LLMemory
        # so the preview shape is unchanged for them.
        "vendor_dispatches": vd_n,
        "memory_events": memev_n,
        "lqa_results": lqa_n,
        "segment_post_edits": pe_n,
        "query_threads": q_thread_n,
        "query_posts": q_post_n,
        "members": _n(select(ProjectMember.id).where(ProjectMember.project_id == project_id)),
        "glossary_entries": _n(
            select(GlossaryEntry.id).where(GlossaryEntry.project_id == project_id)
        ),
        "project_workflows": _n(select(Workflow.id).where(Workflow.project_id == project_id)),
        "context_cards": _n(
            select(ContextProfile.id).where(ContextProfile.project_id == project_id)
        ),
        "jobs": len(_project_job_ids(session, project_id, doc_ids)),
        # Key-based projects (SPEC-key-based-projects 2.2/2.3). Counted for EVERY
        # project: on a document project both are 0, so the preview shape is unchanged
        # for them, and on a key project the confirm dialog stops under-reporting what
        # the delete destroys. `string_keys` counts tombstoned keys too -- a soft
        # delete is still a row this cascade removes.
        "string_keys": _n(select(StringKey.id).where(StringKey.project_id == project_id)),
        "key_changelog": _n(select(KeyChangeLog.id).where(KeyChangeLog.project_id == project_id)),
        # Per-string in-context images (Batch 14): counted for EVERY project (0 on a
        # document project + a key project with no images), so the confirm dialog stops
        # under-reporting -- the delete reaps their blobs + rows (LAW-IMG-15). project_id
        # is the denormalized tenant anchor.
        "key_images": _n(select(KeyImage.id).where(KeyImage.project_id == project_id)),
        # Batch 15 T2: empty-delivery approval records (0 on any project that never held a
        # TM-only empty delivery), reaped in step 2c before the run/document deletes.
        "delivery_empty_approvals": _n(
            select(DeliveryEmptyApproval.id).where(DeliveryEmptyApproval.project_id == project_id)
        ),
        # Attached-TM / pinned-glossary links (feedback item 5). BOTH tables carry a
        # real FK to project.id, so both must be counted here and removed in step 8d
        # or the final DELETE fails after earlier steps have committed.
        #
        # THREE distinct counts, because they are three different losses and the
        # confirm dialog should not blur them:
        #   tm_sources_attached   -- links this project OWNS (memory it borrows). Lost
        #                            with the project; nobody else notices.
        #   tm_source_uses        -- links pointing AT this project (OTHER projects
        #                            borrow ITS memory). Those projects silently lose
        #                            coverage, so this number is the one worth reading
        #                            before confirming.
        #   glossary_pins         -- glossaries this project pins. The GLOSSARIES
        #                            themselves survive; only the pins go.
        # None of the three BLOCKS the delete. Blockers are reserved for INDEPENDENT
        # referrers that a delete would orphan (Connection / RoutingRule); a link row is
        # owned by the pair it joins, and "someone borrows my memory" is not a veto over
        # deleting my project -- it degrades exactly as a manual detach would.
        "tm_sources_attached": _n(
            select(ProjectTmSource.id).where(ProjectTmSource.project_id == project_id)
        ),
        "tm_source_uses": _n(
            select(ProjectTmSource.id).where(ProjectTmSource.source_project_id == project_id)
        ),
        "glossary_pins": _n(
            select(ProjectGlossary.id).where(ProjectGlossary.project_id == project_id)
        ),
        # Advanced tier (gathered above). Reaped in step 8f through the advanced service's own
        # seam, BEFORE the project DELETE their FKs would otherwise block.
        #
        # `advanced_quality_findings` is QUALIFIED deliberately: it counts ONLY findings parented
        # to this project's ADVANCED runs. `retention.delete_document` reaps the document- and
        # segment-parented arms separately at step 4 and reports them under its own
        # `quality_findings` key, which this cascade never merges. Today the writer only ever sets
        # a run parent so the other arms are empty in practice, but an unqualified name here would
        # silently promise a total this dict does not carry the day that changes.
        "advanced_runs": len(adv_run_ids),
        "advanced_missions": _n(
            select(AdvancedMission.id).where(AdvancedMission.project_id == project_id)
        ),
        "advanced_staged_targets": len(adv_staged_ids),
        "advanced_transcript_entries": adv_te_n,
        "advanced_quality_findings": adv_qf_n,
        # Per-project spend cap (step 8g) and LLMemory suggestions (un-scoped in step 8e).
        # Both carry a real FK to project.id and BOTH left the identical husk before this train.
        "project_budgets": _n(
            select(ProjectBudget.id).where(ProjectBudget.project_id == project_id)
        ),
        "memory_suggestions": _n(
            select(MemorySuggestion.id).where(MemorySuggestion.project_id == project_id)
        ),
    }
    cascade["total"] = sum(cascade.values())
    connections = _n(select(Connection.id).where(Connection.project_id == project_id))
    routing_rules = _n(select(RoutingRule.id).where(RoutingRule.project_id == project_id))
    # LOW-2: forecast the step-3c-pre workflow_memory_suggestions blocker so the preview does
    # NOT report a suggestion-targeted project as deletable (the /api dry-run and CLI preview
    # would otherwise say deletable and the delete then 400s -- the same preview/outcome
    # asymmetry LOW-4 closed on the workflow surface). A LAW-2 MemorySuggestion targeting a
    # project-scoped workflow is a permanent audit tombstone (NOT NULL FK, never deleted), so
    # delete_project fails closed at step 3c. Counted here and summed into `total`; the actual
    # refusal message stays the tailored archive one raised at step 3c (the referrer-only
    # short-circuits below key on connections+routing, not on this total -- see delete_project).
    workflow_memory_suggestions = (
        _n(select(MemorySuggestion.id).where(MemorySuggestion.workflow_id.in_(wf_ids)))
        if wf_ids
        else 0
    )
    return {
        "project_id": project_id,
        "project_name": project.name,
        "blockers": {
            "connections": connections,
            "routing_rules": routing_rules,
            "workflow_memory_suggestions": workflow_memory_suggestions,
            "total": connections + routing_rules + workflow_memory_suggestions,
        },
        "cascade": cascade,
    }


def content_disposition_attachment(name: str) -> str:
    """Build a full ``Content-Disposition: attachment`` header VALUE that is safe for a
    NON-ASCII (Unicode) filename, per RFC 6266 / RFC 5987.

    HTTP response headers are latin-1-encoded on the ASGI wire, so a raw Unicode filename
    placed in a bare ``filename="..."`` parameter raises ``UnicodeEncodeError`` at send time
    -> an unhandled 500 (probe-proven with a CJK or emoji document name; rename ACCEPTS such
    names, only ``"``/CRLF are rejected). RFC 6266 solves it with TWO parameters: an ASCII
    ``filename=`` fallback for legacy clients, AND a ``filename*=UTF-8''<percent-encoded>``
    parameter carrying the true UTF-8 name (modern clients prefer ``filename*``). Both are
    latin-1-safe: the fallback is pure ASCII and the ext-value is percent-encoded ASCII.

    Security is preserved from ``header_safe_filename``: the ASCII fallback runs through it
    (``"``/CRLF/C0-DEL -> ``_``) then drops any remaining non-ASCII to ``_`` so the quoted
    ``filename="..."`` can never break out or carry an un-encodable byte; the ``filename*``
    ext-value is percent-encoded via ``quote(safe="")``, which encodes ``"``/CR/LF/controls
    (and every non-attr-char) to ``%XX``, so it cannot inject a parameter or a header line
    either. An empty result falls back to ``download``."""
    from urllib.parse import quote

    safe = header_safe_filename(name)  # "/CRLF/control -> _
    ascii_fallback = "".join(c if c.isascii() else "_" for c in safe).strip() or "download"
    ext_value = quote(str(name), safe="")  # RFC 5987 ext-value (UTF-8 %XX)
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{ext_value}"


# --- document filename safety (issue #142) -----------------------------------
#: BOTH path separators, always checked together: a name typed on (or sent from) one
#: platform is stored and later composed on the other, so `\` matters on POSIX and `/`
#: matters on Windows. A UNC prefix (`\\srv\share`) is caught by the same rule.
#: A `:` is handled separately below -- it is the drive prefix (`C:x`) AND the Windows
#: alternate-data-stream suffix (`report.txt:evil`), and is not legal in a filename.
_FILENAME_SEPARATORS = ("/", "\\")

#: The longest document filename an operator may SET, and the ceiling every sanitised or
#: composed name is held under. Gate round-1 HIGH: a name has to be short enough to OPEN,
#: not merely free of separators. Common filesystems cap ONE path component at 255 bytes
#: (NTFS 255 UTF-16 units, ext4 255 bytes), and a sink composes `<stem>.<locale>.<fmt>`
#: on top of the stored name -- so an accepted 250-char name produced a ~260-char
#: component and an uncaught `OSError [Errno 22] Invalid argument` at `open()`. 200 leaves
#: room for the longest real locale tag plus an extension inside 255 and is still far
#: above any human filename (exactly 200 is ACCEPTED; 201 is refused).
DOCUMENT_FILENAME_MAX = 200


def validate_document_filename(name: str) -> str:
    """THE one validator for an OPERATOR-supplied document filename (rename, on every
    surface). Returns the clean name; raises ValueError with a one-line message.

    Issue #142: the rename guard blocked blank / control / `"` names but PERMITTED
    `/`, `\\`, `..`, `~` and a drive or UNC prefix, so a same-tenant writer could store
    `../../../../Windows/win.ini.txt` as a document filename -- and the CLI's DEFAULT
    export name is composed from the stored filename in the CWD, i.e. an arbitrary
    write (or a PermissionError traceback). `Document.filename` is a DISPLAY name, never
    a location: blob storage is id-keyed. So a name may not carry a path at all.

    The existing rules and their EXACT messages are kept (blank, ASCII control chars ->
    header injection, `"` -> Content-Disposition parameter injection); the path rules are
    added on top. Sinks still basename defensively (`safe_output_stem`) because rows
    renamed BEFORE this guard are not migrated."""
    new_name = (name or "").strip()
    if not new_name:
        raise ValueError("document filename is required.")
    if _CONTROL_CHARS_RE.search(new_name):
        raise ValueError("document filename may not contain control characters.")
    # F3 (MEDIUM, Phase-2): a `"` reaches the download Content-Disposition
    # `filename="..."` and breaks OUT of the quoted-string, injecting header
    # parameters (a spoofed `filename*=`, etc.). Control chars alone weren't
    # enough — the quote is the parameter-injection primitive. Reject it here so
    # both the web and /api rename surfaces are protected at the source; the
    # download seam ALSO neutralises it (header_safe_filename) for rows renamed
    # before this guard. (`;` stays allowed — it is inert inside the quotes.)
    if '"' in new_name:
        raise ValueError("document filename may not contain a double-quote character.")
    if any(sep in new_name for sep in _FILENAME_SEPARATORS):
        raise ValueError(
            "document filename may not contain a path separator ('/' or '\\'): "
            "it is a display name, not a location."
        )
    if new_name.strip(".") == "":  # ".", "..", "...": a path component, not a name
        raise ValueError("document filename may not be a path component ('.' or '..').")
    if ":" in new_name:
        raise ValueError(
            "document filename may not contain ':' (a drive letter or an "
            "alternate-data-stream suffix)."
        )
    if new_name.startswith("~"):
        raise ValueError("document filename may not start with '~' (a home-directory path).")
    # Windows SILENTLY drops trailing dots and spaces, so `x.txt.` and `x.txt` are the same
    # file under two different stored names (and `x.txt ` round-trips to `x.txt`). Normalise
    # instead of refusing -- the operator meant the visible name (gate round-1 LOW).
    new_name = new_name.rstrip(" .")
    if not new_name:
        raise ValueError("document filename is required.")
    if len(new_name) > DOCUMENT_FILENAME_MAX:
        raise ValueError(
            f"document filename may not be longer than {DOCUMENT_FILENAME_MAX} characters "
            f"(got {len(new_name)}): a longer name cannot be opened once a sink composes "
            "an export name on top of it."
        )
    return new_name


def safe_document_filename(name: str, *, fallback: str = "upload") -> str:
    """A filename SUPPLIED BY A MACHINE (an upload's multipart name, a connector's
    remote title) reduced to a safe display name: its LAST path component, with the
    characters `validate_document_filename` refuses neutralised to `_`.

    Sanitise, don't refuse (issue #142 design): a browser that sends `C:\\fakepath\\x.md`
    or a remote system whose title carries a slash is not the operator making a mistake,
    and an ingest must not fail on it. The operator's OWN rename still refuses."""
    raw = _last_path_component(name)
    raw = _CD_UNSAFE_RE.sub("_", raw).replace('"', "_").replace(":", "_").strip()
    if raw.startswith("~"):
        raw = "_" + raw[1:]
    # Windows drops trailing dots and spaces; the rename validator also strips ALL Unicode
    # whitespace at both ends, so loop to a fixed point: whatever an upload stores then
    # passes validate_document_filename unchanged (#614).
    while True:
        trimmed = raw.strip().rstrip(" .")
        if trimmed == raw:
            break
        raw = trimmed
    if not raw or raw.strip(".") == "":
        return fallback
    # #614 (review gate): a stored name is also the stem of every DEFAULT export name
    # (`<stem>.<locale>.<fmt>` in the CWD), so it may not be a Windows device name
    # (`CON.txt` opens the console on Windows 10) or start with a dot (a hidden file on
    # Unix). Prefix `_` and re-truncate to a fixed point (the prefix can cost one char).
    out = _truncate_keeping_ext(_neutralise_device_or_dot(raw), DOCUMENT_FILENAME_MAX, fallback)
    while _neutralise_device_or_dot(out) != out:
        out = _truncate_keeping_ext(_neutralise_device_or_dot(out), DOCUMENT_FILENAME_MAX, fallback)
    return out


# Windows reserves these device names whatever the extension (`nul.tar.gz` included),
# case-insensitively, ignoring spaces before the first dot.
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    | {f"{p}{d}" for p in ("COM", "LPT") for d in "0123456789¹²³"}
)


def _is_windows_device_name(name: str) -> bool:
    """True when `name` would open a Windows device instead of a file."""
    return (name or "").split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_DEVICE_NAMES


def _neutralise_device_or_dot(name: str) -> str:
    """`_` before a name that starts with a dot or is a Windows device name."""
    if name.startswith(".") or _is_windows_device_name(name):
        return "_" + name
    return name


def _last_path_component(name: str) -> str:
    """The last component of `name` under EITHER separator, with trailing dots/spaces
    dropped. A Windows client's name reaches a POSIX server and vice versa, so
    `os.path.basename` alone (which only knows the LOCAL separator) is not enough."""
    raw = str(name or "").strip()
    for sep in _FILENAME_SEPARATORS:
        raw = raw.rsplit(sep, 1)[-1]
    return raw.strip().rstrip(" .")


def _truncate_keeping_ext(name: str, limit: int, fallback: str) -> str:
    """`name` shortened to at most `limit` characters, keeping its extension.

    A machine-supplied name is TRUNCATED, never refused (gate round-1 HIGH, layer 2): a
    remote system with a 400-character title must still ingest, and the extension is what
    every format lookup (`ingest._ext`) and every export composition depends on, so it is
    the part that survives."""
    if len(name) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and stem and len(ext) + 1 < limit:
        keep = limit - len(ext) - 1
        stem = stem[:keep].rstrip(" .")
        return f"{stem}.{ext}" if stem else f"{fallback[: keep or 1]}.{ext}"
    return name[:limit].rstrip(" .") or fallback[:limit]


def safe_output_stem(name: str, *, fallback: str = "document", neutralise: bool = True) -> str:
    """The STEM a caller may compose an output name from: the last path component of
    `name`, extension dropped, length-bounded.

    Defence in depth for rows poisoned BEFORE the rename guard landed (there is no
    migration): every caller that builds `<stem>.<locale>.<fmt>` runs the stored filename
    through this first, so the composed name is a single path component.

    `neutralise=False` keeps the component's own characters (only the PATH and the length
    are dealt with). That is what the download HEADERS pass: their seam,
    `content_disposition_attachment`, already neutralises the quote, the backslash and the
    control characters in the ASCII
    `filename=` fallback AND percent-encodes the true name into `filename*=UTF-8''...`
    (verify_document_cd_injection pins that contract). Replacing those characters HERE
    would silently strip the RFC-6266 ext-value of the real name."""
    if not neutralise:
        base = (
            _truncate_keeping_ext(_last_path_component(name), DOCUMENT_FILENAME_MAX, fallback)
            or fallback
        )
    else:
        base = safe_document_filename(name, fallback=fallback)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = stem.strip().rstrip(" .")
    return stem[:DOCUMENT_FILENAME_MAX] or fallback


def safe_output_name(
    name: str, *suffix_parts: str, fallback: str = "document", neutralise: bool = True
) -> str:
    """THE composer every export sink uses for its DEFAULT output name:
    `<safe stem>.<part>.<part>` as ONE path component that is always openable.

    Gate round-1 HIGH: basenaming alone was not enough. A stored name near the filesystem's
    per-component ceiling (or a pre-fix row carrying 300 characters -- there is no
    migration) produced a composed name past it, and the sink's `open()` raised an uncaught
    `OSError [Errno 22]`. The STEM absorbs the truncation; the suffix parts (the locale and
    the format) never do -- shortening those would produce a file that lies about which
    locale it holds, or that no tool would open. `neutralise=False` is the header caller's
    form -- see safe_output_stem."""
    stem = safe_output_stem(name, fallback=fallback, neutralise=neutralise)
    suffix = "".join("." + str(p) for p in suffix_parts if str(p or "").strip())
    room = DOCUMENT_FILENAME_MAX - len(suffix)
    if room < 1:  # pathological caller (a suffix longer than the whole budget)
        return (stem[:1] or fallback[:1]) + suffix
    stem = stem[:room].rstrip(" .") or fallback[:room]
    return stem + suffix


def rename_document(
    session: Session,
    doc_id: int,
    filename: str,
    *,
    actor: str = "system",
    ip: str = "",
    org_id=None,
) -> "Document | None":
    """Rename a document's filename. Blob storage is id-keyed (<id>.<fmt>), so the
    on-disk upload is unaffected — only the display filename changes. Org-guarded via
    the owning project. Audits document.rename old->new. Returns the Document, or None
    when it doesn't exist / is cross-org; raises ValueError on a blank name, one
    containing ASCII control characters (F1 — the filename reaches the download route's
    Content-Disposition header unescaped, so CR/LF there is a header-injection primitive),
    or one containing a double-quote (F3 — a `"` breaks out of the quoted `filename="..."`
    parameter and injects Content-Disposition parameters), or one carrying a PATH
    (issue #142 — `/`, `\\`, a `..` component, `~`, a drive/stream `:`; the filename is a
    display name that filesystem sinks compose an output path from). Every rule lives in
    `validate_document_filename`, which the web, /api and CLI rename surfaces share."""
    from app.models import Document
    from app.services import audit

    doc = session.get(Document, doc_id)
    if doc is None:
        return None
    project = session.get(Project, doc.project_id)
    if org_id is not None and (project is None or getattr(project, "org_id", None) != org_id):
        return None
    new_name = validate_document_filename(filename)
    old_name = doc.filename
    doc.filename = new_name
    session.add(doc)
    session.commit()
    session.refresh(doc)
    audit.log(
        actor=actor or "system",
        action="document.rename",
        target=f"document {doc.id}",
        detail=f"{old_name!r} -> {new_name!r}",
        ip=ip,
        customer_id=getattr(project, "customer_id", None),
    )
    return doc


def add_target_locale(session: Session, project_id: int, locale: str, *, actor: str = "") -> bool:
    """Add a single TargetLocale to a project if it isn't already present, then seed
    source segments for it on every existing document (living projects, P2 item 4).

    Additive + idempotent (same contract as the Language-Set merge below).
    Validates the locale against the BCP-47 catalog (P2, item 5): a garbage code is
    rejected with InvalidLocaleError; a disabled-but-valid code is accepted (the
    enabled list is a UI-offering filter, not a hard gate). Returns True when a new
    row was added, False when the locale was blank or already present."""
    loc = (locale or "").strip()
    if not loc:
        return False
    valid, invalid = languages_catalog.validate_codes([loc])
    if invalid:
        raise InvalidLocaleError(invalid)
    loc = valid[0]  # canonicalized (es_ES -> es-ES)
    # Dedup on the NORMALIZED key, not the raw string: an existing row may be a
    # legacy / non-canonical spelling of the same locale (a pre-P2 row, or a
    # connector's free-text config that seeds TargetLocale unvalidated — e.g.
    # "ES-es"/"es_ES"). Comparing raw strings would let a canonical add ("es-ES")
    # fork the project into two rows for one locale, which downstream code (segment
    # seeding, TM buckets, per-locale delivery) treats as two distinct locales.
    existing_norm = _project_locale_norms(session, project_id)
    restarted = False
    if languages_catalog.normalize(loc) in existing_norm:
        # A STOPPED language of a connected project comes back (same row, its segments and
        # translations intact); an active one is already present.
        stopped = [
            tl for tl in _project_locale_rows(session, project_id, loc) if tl.stopped_at is not None
        ]
        if not stopped:
            return False
        for tl in stopped:
            tl.stopped_at = None
            session.add(tl)
        session.commit()
        loc = stopped[0].locale
        restarted = True
    else:
        session.add(TargetLocale(project_id=project_id, locale=loc))
        session.commit()
    # Living-projects (P2, item 4): seed segments for the new locale on documents
    # already parsed, so it is immediately translatable.
    for doc in session.exec(select(Document).where(Document.project_id == project_id)).all():
        _seed_locale_segments(session, doc.id, loc)
    _materialize_key_values(session, project_id, actor=actor)
    # Living-projects reopen (P1): adding a locale to a COMPLETED/CLOSED project
    # flips it back to IN_TRANSLATION. Never touches existing targets.
    from app.services import lifecycle

    lifecycle.reopen_if_terminal(session, project_id)
    _locale_changed_on_bound(project_id, loc, actor=actor, added=True, restarted=restarted)
    return True


def is_bound_project(session: Session, project_id: int) -> bool:
    """True when the project has a content source (a ProjectAutomation binding). Its languages
    are then STOPPED rather than deleted (2026-10-13, connected projects)."""
    from app.models import ProjectAutomation

    return (
        session.exec(
            select(ProjectAutomation.id).where(ProjectAutomation.project_id == project_id)
        ).first()
        is not None
    )


def active_target_locales(session: Session, project_id: int) -> list[str]:
    """The project's target languages that are not stopped, in insertion order."""
    return [
        tl.locale
        for tl in session.exec(
            select(TargetLocale)
            .where(TargetLocale.project_id == project_id, TargetLocale.stopped_at.is_(None))
            .order_by(TargetLocale.id)
        ).all()
    ]


def stopped_target_locales(session: Session, project_id: int) -> list[str]:
    """The project's stopped target languages (kept, readable and exportable)."""
    return [
        tl.locale
        for tl in session.exec(
            select(TargetLocale)
            .where(TargetLocale.project_id == project_id, TargetLocale.stopped_at.is_not(None))
            .order_by(TargetLocale.id)
        ).all()
    ]


def _locale_changed_on_bound(
    project_id: int, locale: str, *, actor: str, added: bool, restarted: bool = False
) -> None:
    """Tell the project automation a language was added / stopped. Inert for a project with
    no content source. A failure here never undoes the language change itself."""
    try:
        from app.services import project_automation

        if added:
            project_automation.locale_added(project_id, locale, actor=actor, restarted=restarted)
        else:
            project_automation.locale_stopped(project_id, locale, actor=actor)
    except Exception:
        import logging

        logging.getLogger("loc_tms.content").exception(
            "Project automation bookkeeping failed for a language change on project %s", project_id
        )


def remove_locale_preview(session: Session, project_id: int, locale: str) -> dict | None:
    """Read-only, itemized view of what removing `locale` from the project WOULD do:
    what is DELETED (the TargetLocale row(s), every Segment for that locale across the
    project's documents + their editor state and LQA results, any per-pair provider
    override for that target) and what is PRESERVED (TM + glossary — durable assets
    that survive a re-add — and workflow-run HISTORY). Also reports the blocker, if
    any: the LAST-LOCALE rule, or the PERMANENCE rule — a locale
    that already has translated content (see translated_target_locales) can never be
    removed, only one that hasn't been translated yet. (A CONNECTED project's language is
    stopped instead of removed: mode "stop", nothing deleted, the permanence rule does not
    apply.) Returns None if the project
    doesn't exist or the locale isn't on it. Never mutates. (The inverse of
    add_target_locale's segment seeding.) This is the ONE place both blockers are
    computed; remove_target_locale re-derives this preview fresh right before it
    deletes anything, so every surface (web confirm page, CLI pre-check, /api) reads
    the SAME verdict and none can disagree with what's actually enforced."""
    from app.models import (
        FullTextTM,
        GlossaryEntry,
        LQAResult,
        QueryPost,
        SegmentPostEdit,
        SegmentTM,
        WorkflowRun,
    )

    project = session.get(Project, project_id)
    if project is None:
        return None
    rows = _project_locale_rows(session, project_id, locale)
    if not rows:
        return None
    want = languages_catalog.normalize(locale)
    shown = rows[0].locale  # the canonical/stored spelling to echo back

    def _n(stmt) -> int:
        return len(session.exec(stmt).all())

    doc_ids = list(session.exec(select(Document.id).where(Document.project_id == project_id)).all())
    # Segments for THIS locale (normalized match, to catch legacy spellings).
    seg_ids: list[int] = []
    if doc_ids:
        for sg in session.exec(select(Segment).where(Segment.document_id.in_(doc_ids))).all():
            if languages_catalog.normalize(sg.target_locale) == want:
                seg_ids.append(sg.id)
    lqa_n = _n(select(LQAResult.id).where(LQAResult.segment_id.in_(seg_ids))) if seg_ids else 0
    # Post-edit episode rows for THIS locale's segments (§3.2.6) — counted so the confirm
    # dialog does not understate the delete.
    pe_n = (
        _n(select(SegmentPostEdit.id).where(SegmentPostEdit.segment_id.in_(seg_ids)))
        if seg_ids
        else 0
    )
    # Query threads reaped with this locale (§3.5): threads on a doomed segment or whose
    # target_locale matches. Count them + their posts so the confirm dialog is honest.
    q_thread_ids = _locale_query_thread_ids(session, project_id, seg_ids, want)
    q_thread_n = len(q_thread_ids)
    q_post_n = (
        _n(select(QueryPost.id).where(QueryPost.thread_id.in_(q_thread_ids))) if q_thread_ids else 0
    )
    pair_n = len(
        [
            pp
            for pp in session.exec(
                select(ProjectPairProvider).where(ProjectPairProvider.project_id == project_id)
            ).all()
            if languages_catalog.normalize(pp.target_locale) == want
        ]
    )
    run_n = _n(select(WorkflowRun.id).where(WorkflowRun.document_id.in_(doc_ids))) if doc_ids else 0
    tm_n = len(
        [
            e
            for e in session.exec(select(SegmentTM).where(SegmentTM.project_id == project_id)).all()
            if languages_catalog.normalize(e.target_locale) == want
        ]
    )
    ft_n = len(
        [
            e
            for e in session.exec(
                select(FullTextTM).where(FullTextTM.project_id == project_id)
            ).all()
            if languages_catalog.normalize(e.target_locale) == want
        ]
    )
    gl_n = len(
        [
            g
            for g in session.exec(
                select(GlossaryEntry).where(GlossaryEntry.project_id == project_id)
            ).all()
            if g.target_locale and languages_catalog.normalize(g.target_locale) == want
        ]
    )
    # Connected projects: a language of a project with a content source is STOPPED, not
    # deleted (the row, its segments, TM and exports stay; runs stop using it). The last-locale
    # rule counts ACTIVE languages only; a language that is already stopped can still go through
    # the ordinary removal below (it is no longer feeding a run).
    total_active = _n(
        select(TargetLocale.id).where(
            TargetLocale.project_id == project_id, TargetLocale.stopped_at.is_(None)
        )
    )
    active_in_rows = len([tl for tl in rows if tl.stopped_at is None])
    stop_mode = active_in_rows > 0 and is_bound_project(session, project_id)
    is_last = active_in_rows > 0 and total_active - active_in_rows <= 0
    # PERMANENCE rule: once translated, never removable. Checked
    # even when is_last is already True (last-locale wins the message, since it's
    # checked first below) — a caller resolving this predicate directly still gets an
    # accurate is_translated flag either way.
    is_translated = want in translated_target_locales(session, project_id, doc_ids)
    if is_last:
        blocked, reason = (
            True,
            (
                f"cannot remove {shown} - it is the project's only target locale. A project "
                "must keep at least one target locale."
            ),
        )
    elif is_translated and not stop_mode:
        blocked, reason = (
            True,
            (
                f"cannot remove {shown} - it has translated content (a segment with target "
                "text, and/or a translation run). Once a locale has been translated its "
                "documents are permanent; removal is only possible before translation."
            ),
        )
    else:
        blocked, reason = False, None
    deletes = {
        "target_locale_rows": len(rows),
        "segments": len(seg_ids),
        "lqa_results": lqa_n,
        "segment_post_edits": pe_n,
        "query_threads": q_thread_n,
        "query_posts": q_post_n,
        "pair_provider_overrides": pair_n,
    }
    # round-3 L5: the DRY-RUN Advanced erasure counts, folded in the SAME `advanced_<k>` shape
    # remove_target_locale returns, so the confirm dialog reports EXACTLY the Advanced rows the
    # removal will sever (M3 discloses the blast radius -- the preview must not under-report it).
    # Read-only: reap_document_content(dry_run=True) SELECTs + counts but writes nothing. Lazy
    # import (the HARD RULE + the retention idiom).
    if seg_ids:
        from app import extensions as _extensions

        _adv_preview = _extensions.run_reapers(
            "document_content", session, seg_ids=seg_ids, project_id=project_id, dry_run=True
        )
        deletes.update({f"advanced_{k}": v for k, v in _adv_preview.items() if v})
    preserves = {
        "tm_segments": tm_n,
        "tm_fulltext": ft_n,
        "glossary_entries": gl_n,
        "workflow_runs": run_n,
    }
    if stop_mode:
        # Nothing is deleted: report the same keys with zero, and what stays.
        preserves = {"segments": len(seg_ids), **preserves}
        deletes = dict.fromkeys(deletes, 0)
    return {
        "project_id": project_id,
        "project_name": project.name,
        "locale": shown,
        "deletes": deletes,
        "preserves": preserves,
        "is_translated": is_translated,
        "blocked": blocked,
        "blocked_reason": reason,
        # "stop": the project is connected, so removing this language only STOPS it (the next
        # run no longer pulls, translates or writes it); "delete": the ordinary removal.
        "mode": "stop" if stop_mode else "delete",
    }


def apply_language_set_to_project(session: Session, project_id: int, ls, *, actor: str = "") -> int:
    """Add a TargetLocale row for every locale in the Language Set `ls` that the
    project doesn't already have. Additive merge only — never touches existing
    TargetLocale rows (or their segments/translations), and re-applying is
    idempotent. Returns the number of locales added.

    (The caller resolves + validates `ls` via app.services.languages.get and
    still owns the audit-log line, matching the route it replaced.)"""
    # Dedup on the NORMALIZED key (see add_target_locale): a set's stored code and
    # an existing TargetLocale row may be non-canonical spellings of the same locale
    # (e.g. a pre-P2 set holding "es_ES" over a project row "es-ES"). Raw-string
    # comparison would fork one locale into two rows; downstream code treats those as
    # two distinct locales (double segments/TM/delivery).
    existing_norm = _project_locale_norms(session, project_id)
    new_locales = []
    restarted: set[str] = set()
    for loc in languages.parse_locales(ls.target_locales):
        if languages_catalog.normalize(loc) not in existing_norm:
            session.add(TargetLocale(project_id=project_id, locale=loc))
            existing_norm.add(languages_catalog.normalize(loc))
            new_locales.append(loc)
        else:
            # a language a connected project had stopped comes back with the set
            for tl in _project_locale_rows(session, project_id, loc):
                if tl.stopped_at is not None:
                    tl.stopped_at = None
                    session.add(tl)
                    new_locales.append(tl.locale)
                    restarted.add(tl.locale)
    session.commit()
    # Living-projects (P2, item 4): seed segments for each newly-added locale on the
    # project's already-parsed documents (a set is applied grandfathered — its codes
    # were validated at creation, so we don't re-validate here).
    if new_locales:
        docs = session.exec(select(Document).where(Document.project_id == project_id)).all()
        for loc in new_locales:
            for doc in docs:
                _seed_locale_segments(session, doc.id, loc)
        _materialize_key_values(session, project_id, actor=actor)
        # Living-projects reopen (P1): if this merge actually added a locale to a
        # COMPLETED/CLOSED project, flip it back to IN_TRANSLATION.
        from app.services import lifecycle

        lifecycle.reopen_if_terminal(session, project_id)
        for loc in new_locales:
            _locale_changed_on_bound(
                project_id, loc, actor=actor, added=True, restarted=loc in restarted
            )
    return len(new_locales)
