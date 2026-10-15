"""Export gating, held-locale policies, and quarantine exceptions.

Split from app.services.export as part of single-responsibility refactoring.
Owns:
  * TagExportQuarantine and related delivery-refusal exceptions
  * held_locale_reason and gate_held_reason checks
  * assert_deliverable assertion for document locale readiness
  * substitution_detail reporting helpers and kind constants

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models import Document, Segment, SegStatus


class TagExportQuarantine(Exception):
    """AI Tagging 1.6 — a FULL_TEXT export was refused GLOBALLY because the translated
    body's paragraph structure desynced from the source (a merged/reordered blank line,
    or a per-paragraph inline-token set that no longer matches its source paragraph).

    Because per-paragraph token ids restart at 1 (docx_filter), a FULL_TEXT blob holds
    many colliding `⟦r1⟧` spans from different paragraphs, and the per-segment commit
    gate — which sees the whole blob as ONE segment — is structurally BLIND to a
    positional shift whose whole-body multiset is still intact.

    THE SCOPE OF THAT ARGUMENT, stated precisely (GitHub #42, item C). It describes the
    GUARD-A failure mode — the paragraph COUNT desync. One merged blank line shifts the
    positional `parts[idx]`→unit map for EVERY later paragraph, so no per-paragraph view
    of that document is trustworthy and a per-paragraph revert-to-source would ship a
    half-translated doc that looks done. Guard A therefore keeps its EXCLUSIVE claim on
    the global quarantine: `kind == "count"` offers no `paragraphs`, no split view, no
    revert and no override, and `allow_source_fallback` does not touch it.

    GUARD B (`kind == "tokens"`) is the case #42 found in real data (doc 44: six dropped
    paired tokens, all in paragraph 0 of 5). The paragraph count aligns, so the map is
    exact and the damage is LOCAL — and the deterministic repair rung bails on a paired
    source by contract, so nothing could ever recover it. That case gets a per-paragraph
    diagnosis, a reviewer-driven revert, and (only on an explicit per-export request,
    under a share ceiling) a source fallback.

    Typed attributes, so a caller can do more than `str(exc)`:
      `doc_id`, `locale`;
      `kind` — "count" (guard A: a TAGGED paragraph-COUNT desync, OR the tagless
        honest-failure `parts > units` direction — extra translated paragraphs make the
        positional map untrustworthy; both offer no per-paragraph view and no override),
        "tokens" (guard B), "untranslated" (the tagless honest-failure `parts < units`
        direction OR a blank stored body: the translated body has FEWER real paragraphs
        than the source, so exporting would silently substitute ENGLISH source — like
        "count" it is UNCONDITIONAL, no `allow_source_fallback` override, because a count
        desync leaves no trustworthy positional map to substitute against), "share_ceiling"
        (an explicit source-fallback override for TAGGED guard-B damage refused for
        covering too much of the body), or "segments" (the SEGMENTED
        `export_tag_damage_policy="quarantine"` path);
      `paragraphs` — for "tokens"/"share_ceiling", one dict per BLOCKING paragraph:
        {index, p_index, reason, missing, extra, source_text, target_text}. Guard B
        scans EVERY paragraph and raises ONCE, so an operator sees the whole delta
        instead of fixing one and hitting the next. `reason` is `quality.tag_damage`'s
        own verdict string and is the AUTHORITATIVE field: missing/extra are BOTH EMPTY
        for the damage classes that keep the token multiset intact (emptied span,
        malformed nesting), so a surface that renders only the delta shows an operator
        nothing.

    Conservative quarantine semantics (advisory dropped, GitHub #42): ANY tag
    divergence — including a shape-preserving reorder — is damage. The recovery surface
    (`fulltext_paragraphs`) is the mitigation.

    `str(exc)` stays BYTE-COMPATIBLE with 1.6 (first sentence identical, at most a
    trailing count/damage appended), so a handler can always fall back to `str(exc)`.
    delivery.py and projects.py do exactly that. documents.py (web) and cmd_doc.py
    additionally SWITCH ON `kind` to attach a way-out (a paragraphs view / an
    `allow_source_fallback=1` export-anyway URL) for the recoverable tagged "tokens"
    case — the no-way-out kinds ("count", "untranslated", "share_ceiling", "segments")
    get the message alone. api.py (/api) does NOT switch: it passes `kind` and
    `paragraphs` through in the 409 body for the client to interpret."""

    def __init__(
        self,
        message: str,
        *,
        doc_id: int | None = None,
        locale: str | None = None,
        kind: str = "tokens",
        paragraphs: list | None = None,
    ):
        super().__init__(message)
        self.doc_id = doc_id
        self.locale = locale
        self.kind = kind
        self.paragraphs: list[dict] = list(paragraphs or [])


# --- the substitution report channel (#41 / #42) ----------------------------------
# ONE entry shape, self-describing, for every surface: {unit, index, kind}.
#
# The channel used to be a bare `list[int]` documented as "order_index ints" — and the
# FULL_TEXT path extended the SAME list with PARAGRAPH indices, so `index 0` meant
# "segment 0" or "paragraph 0" depending on a document mode the reader could not see
# from the record. And every surface wrote `detail=f"substituted={len(rep)}"`, so an
# EXPLICIT `allow_source_fallback` force-export was indistinguishable from a routine
# automatic substitution — retiring the whole justification for the override (that it
# is explicit and reported, never a default). Both facts (`unit`, `kind`) now travel IN
# the record, classified HERE at the seam that performs the substitution.
SUB_AUTOMATIC = "automatic"  # #41 policy: a tag-damaged SEGMENT fell back
SUB_EXPLICIT = "explicit_override"  # #42 C-3: a per-export, operator-requested fallback


def _sub(unit: str, index: int, kind: str) -> dict:
    return {"unit": unit, "index": index, "kind": kind}


def substitution_detail(report: list | None) -> str:
    """The ONE ASCII rendering of a substitution report — audit `detail`, CLI warnings,
    the ZIP manifest.

    `substituted=2 kind=automatic unit=segment index=3,7`. "" for an empty/None report,
    so a clean export's audit row stays byte-identical to pre-#41. Grouped by
    (kind, unit) rather than assumed uniform: one document is either SEGMENTED or
    FULL_TEXT, but a bundle-level caller may hand us several documents' entries."""
    entries = list(report or [])
    if not entries:
        return ""
    groups: dict[tuple, list] = {}
    for e in entries:
        if not isinstance(e, dict):  # defensive: never let an audit write crash
            groups.setdefault(("", ""), []).append(e)
            continue
        groups.setdefault((e.get("kind") or "", e.get("unit") or ""), []).append(e.get("index"))
    out = ["substituted=%d" % len(entries)]  # noqa: UP031
    for (kind, unit), idxs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        out.append(
            "kind={} unit={} index={}".format(
                kind or "unknown", unit or "unknown", ",".join(str(i) for i in idxs[:20])
            )
        )
    return " ".join(out)


class ExportHeldLocale(Exception):
    """chip task_9c665111 / AUDIT-phase2 FINDING 8: an export was refused because the
    requested locale is HELD -- it carries NO committed translation at all, so
    build_translated_file would emit a 100%-SOURCE file that reads as a finished
    deliverable (the exact trap the Phase-2 demo hit: a fr-FR run paused at human_review,
    every segment left `new`/uncommitted, and `download?locale=fr-FR` returned English
    source with a 200). A DELIVERY guard (repo law K1: the export guard is a delivery
    guard, never a storage guard) -- it refuses the DELIVERY, touching no stored state.

    Distinct from TagExportQuarantine (which is about inline-tag DAMAGE on a translated
    doc): this fires only when there is nothing to deliver. A PARTIAL delivery (some
    segments translated, some source-fallback) is a legitimate, maintainer-approved shape and
    is NOT held. Typed `doc_id`/`locale` so a caller can do more than str(exc)."""

    def __init__(self, message: str, *, doc_id=None, locale=None):
        super().__init__(message)
        self.doc_id = doc_id
        self.locale = locale


class ExportEmptyTargetsHeld(Exception):
    """Batch 15 T2 (SPEC-tm-match-step-config §9, D15-6): an export was refused because the
    deliverable is TM-only-provenanced (some WorkflowRun stamped `used_tm_match`) and still
    carries EMPTY (untranslated) targets that NO human has approved for delivery. Distinct
    from `ExportHeldLocale` (the classic ALL-empty source-fallback hold): this fires on a
    PARTIAL deliverable too, is NOT bypassable by `allow_held` (LAW-TM-14), and its way out
    is the AUDITED human approval that ships the empties as empty strings -- never the
    source-filled file.

    A DELIVERY guard only (repo law K1): it refuses the delivery, touching no stored state.
    Carries the target identity (`doc_id` for document mode, `project_id` for key/project
    mode), the `locale`, and the empty identity/source list (`empties`) for the surfaces to
    display. `empties` is built LAZILY by the surface that renders the refusal (never on the
    bundle skip path, N8)."""

    def __init__(self, message: str, *, doc_id=None, project_id=None, locale=None, empties=None):
        super().__init__(message)
        self.doc_id = doc_id
        self.project_id = project_id
        self.locale = locale
        self.empties = empties or []


class ExportMissingBlob(OSError):
    """Issue #54 L3: the document's ORIGINAL upload blob is ABSENT from the BlobStore (a
    deleted `uploads/` entry, a vanished path component), so the format-preserving export
    cannot rebuild the file (there is no source skeleton to inject translations into).
    Before this class existed it surfaced as a raw `FileNotFoundError` -> HTTP 500 with a
    traceback. It is an operator-facing 4xx condition ("original file missing"), not a
    server fault: nothing the export can do recovers a lost source blob.

    STRICTLY the ABSENT case (gate review F4). A blob that is PRESENT but unreadable
    (truncated zip, wrong/rotated at-rest key) is a DIFFERENT condition — see
    `ExportUnreadableBlob` — and a genuine infrastructure fault (a `PermissionError`, a
    disk IO error) is NEITHER: it keeps its 5xx rather than being demoted to a misleading
    "file missing" 409.

    SUBCLASSES `OSError` DELIBERATELY: the bundle / project-export paths (delivery.py,
    projects.py, api_cli.py) already turn a missing-blob `OSError` into a clean 409, and
    inheriting keeps every one of those handlers working unchanged while the single-
    document surfaces (web / api / cli) catch the TYPED class first for a sharper
    "original file missing" message. Typed `doc_id`/`fmt` so a caller can render without
    string-parsing."""

    def __init__(self, message: str, *, doc_id=None, fmt=None):
        super().__init__(message)
        self.doc_id = doc_id
        self.fmt = fmt


class ExportUnreadableBlob(Exception):
    """Issue #54 L3 / gate review F4: the document's original upload blob is PRESENT but
    cannot be decoded — a truncated/corrupt archive (`zipfile.BadZipFile` from a docx/xlsx
    filter) or a failed at-rest decrypt (`cryptography.fernet.InvalidToken`, a rotated or
    wrong ENCRYPTION_KEY). Distinct from `ExportMissingBlob` (the file is THERE, it is just
    not usable), and reported with a distinct "original file unreadable / corrupt" message
    so an operator can tell "restore the upload" from "the storage is broken". An honest
    4xx like the missing case — the export cannot proceed and no retry helps.

    A plain `Exception` (NOT OSError): corruption is not an OS fault, and the single-
    document surfaces catch it explicitly. The bundle paths surface it as they did before
    (unchanged), which is out of F4's single-document scope."""

    def __init__(self, message: str, *, doc_id=None, fmt=None):
        super().__init__(message)
        self.doc_id = doc_id
        self.fmt = fmt


def held_locale_reason(session: Session, doc: Document, locale: str) -> str | None:
    """A refusal string when `locale` is HELD for `doc` (no committed translation at all,
    so an export would be pure source), else None (deliverable, possibly partial).

    "Committed translation" = any segment for the locale carrying a non-empty
    target_text -- which is exactly what build_translated_file emits instead of source
    (`text = s.target_text or s.source_text`). A needs_review segment carries a target
    and so is delivered (the maintainer's 2026-08-03 call: needs_review values ARE allowed in
    build files); a `new`/uncommitted segment (a paused correction loop, an
    untranslated locale) carries none. So a locale where NOT ONE segment has a target is
    held -- source everywhere -- and is refused; one committed target makes it a
    deliverable. Never touches stored state; a delivery-time-only check.

    L4: a PROJECTED query (only target_text, not whole ORM Segment rows) that
    short-circuits in Python -- cheap even in delivery.export_project's per-(doc, locale)
    loop, while keeping exact `.strip()` semantics (SQL TRIM only handles spaces)."""
    rows = session.exec(
        select(Segment.target_text, Segment.source_text).where(
            Segment.document_id == doc.id, Segment.target_locale == locale
        )
    ).all()
    if not rows:
        # No segments for this locale at all -> not this guard's job (the routes' own
        # "locale not found" 404 covers it; a bundle simply source-echoes an absent doc).
        return None
    if any((t or "").strip() for t, _src in rows):
        # Some committed translation: deliverable, unless the locale's LAST translate run
        # ended with failed segments that are still untranslated (#612).
        missing = sum(1 for t, src in rows if (src or "").strip() and not (t or "").strip())
        if missing:
            return _failed_run_hold_reason(doc, locale, missing)
        return None
    return (
        f"locale {locale} is held: it has no delivered translation yet (every "
        f"segment is untranslated), so an export would be entirely source text. "
        f"Finish or resume the run and translate/approve the segments first."
    )


def _failed_run_hold_reason(doc: Document, locale: str, missing: int) -> str | None:
    """#612: a refusal string when the locale's most recent translate run ended FAILED
    with failed segments (an engine outage mid-run) AND `missing` units are still
    untranslated, else None. LIVE, like the quality-gate hold: nothing is stored, so it
    clears itself when a later run finishes cleanly or the missing units get a target
    (a re-run, an editor save). `allow_held` ships the source-filled file explicitly,
    exactly as for the all-untranslated hold."""
    from app.services import jobs

    info = jobs.last_failed_translate_run(doc.id, locale)
    if info is None:
        return None
    first = f" (first engine error: {info['first_error']})" if info.get("first_error") else ""
    return (
        f"locale {locale} is held: the last translation run failed for {info['errors']} "
        f"segment(s){first}, and {missing} unit(s) are still untranslated source text. "
        f"Retry failed segments (run translate again; it picks up only the missing units) "
        f"before exporting."
    )


def gate_held_reason(session: Session, doc: Document, locale: str) -> str | None:
    """A refusal string when `locale` is HELD FOR (doc) BY A QUALITY GATE, else None.

    ISSUE #95 (gate C round 1, H1). A classic run whose LQA correction loop ended with the
    gate's standing verdict still FAILED does not deliver that text as finished work: the
    orchestrator lands every unit it produced NEEDS_REVIEW with a
    `quality.GATE_HOLD_FLAG_TYPE` qa_flag, and this predicate turns that into an honest
    export refusal until a human has looked.

    THE HOLD IS LIVE, NOT ARMED -- exactly like `tm_delivery`'s empty gate. It is derived
    from the CURRENT segments, so it clears itself three ways with no extra route and no
    approval table: a reviewer APPROVES (or EDITS) the segment in the CAT editor / review
    queue -- the reviewer approval the hold is waiting for -- and that unit stops holding;
    a later run that reaches its write-back cleanly CLEARS the flag (the stale-badge rule
    in `quality.clear_gate_hold_flags`); and a document with no flagged unit left is
    deliverable again.

    NOT BYPASSABLE BY `allow_held`. `allow_held` means "ship the source-filled file
    anyway" for the classic no-translation hold; this hold is about text a judge FAILED,
    which is precisely the thing an operator must not wave through from a query string.
    Consulted as a COMPANION alongside `held_locale_reason` at every delivery site, placed
    BEFORE it so the classic override cannot reach past it -- the same placement rule
    §9.2 gives the TM-only empty gate.

    A PROJECTED query (status + qa_flags only, never whole ORM rows), mirroring
    `held_locale_reason`'s L4 shape."""
    # #576: a locale that is only partly cloned onto the document (a chunked add-a-language
    # seed is running, or crashed before it finished) must not be delivered: it would ship
    # source text for the missing rows. Not bypassable with allow_held, like the hold below.
    from app.services import locale_seed

    if locale_seed.in_progress(doc, locale):
        return (
            f"locale {locale} is still being added to this document (some segments are not "
            "created yet). Retry in a moment; if it stays like this, start a translation "
            "for the locale to finish the setup. This hold is not bypassable with allow_held."
        )
    return _gate_held_reason_for_docs(session, [doc.id], locale, unit="segment")


def gate_held_reason_for_project(session: Session, project_id: int, locale: str) -> str | None:
    """The KEY/project-mode twin of `gate_held_reason` (§9.2's mode split).

    A key project's values ARE Segments, living in the project's INTERNAL `fmt="keys"`
    documents, and a key workflow runs through the same segmented orchestrator loop -- so a
    correction loop that ends held flags those value segments exactly as it does a
    document's. Without this the whole-project key export (the canonical TM-only delivery)
    would ship the very text the document export refuses. LAZY, doc-shaped: no `keys`
    import here (P10 keys-inertness -- this module is imported on pure-document installs)."""
    key_doc_ids = session.exec(
        select(Document.id).where(Document.project_id == project_id, Document.fmt == "keys")
    ).all()
    if not key_doc_ids:
        return None
    return _gate_held_reason_for_docs(session, list(key_doc_ids), locale, unit="key value")


def gate_held_segments(session: Session, doc: Document, locale: str, *, limit: int = 10) -> dict:
    """The segments a quality-gate hold is waiting on, for the web refusal page (#593).

    Same predicate as `_gate_held_reason_for_docs` (not approved/edited AND carrying a
    `quality.GATE_HOLD_FLAG_TYPE` flag), but returns the units themselves: `{"total": N,
    "rows": [{order_index, source_text, target_text, step}], "more": N - len(rows)}`. The
    first `limit` by order_index; the page links to the editor for the rest. Read-only; the
    gate itself is unchanged. A locale seed still in progress has no flagged rows (total 0)."""
    from app.services import quality

    segs = session.exec(
        select(Segment)
        .where(Segment.document_id == doc.id, Segment.target_locale == locale)
        .order_by(Segment.order_index)
    ).all()
    rows: list[dict] = []
    total = 0
    for sg in segs:
        if sg.status in (SegStatus.APPROVED, SegStatus.EDITED):
            continue
        for f in sg.qa_flags or []:
            if isinstance(f, dict) and f.get("type") == quality.GATE_HOLD_FLAG_TYPE:
                total += 1
                if len(rows) < limit:
                    rows.append(
                        {
                            "order_index": sg.order_index,
                            "source_text": sg.source_text or "",
                            "target_text": sg.target_text or "",
                            "step": str(f.get("step") or "").strip(),
                        }
                    )
                break
    return {"total": total, "rows": rows, "more": max(0, total - len(rows))}


def _gate_held_cause_clause(causes: set[str], named: str) -> str:
    """The middle clause of the export refusal, branched on the hold's CAUSE.

    GATE C ROUND 3, M2. One fixed sentence described the round-1 endings for every cause,
    on the LOUDEST operator-facing surface of the hold (the web/`/api` 409 body, `doc
    export`'s `export refused: <this>`, the bundle skip reason, the key-export refusal).
    Two of those words were false for the new causes: there is no "correction loop" for
    `unrepaired` (that ending is DEFINED by the absence of one) and no FAILING verdict at
    all for `judge_unavailable` -- it told an operator a judge had failed text no judge had
    judged. The run-level record has said the honest thing since round 2; this is the same
    sentence at the delivery seam.

    A MIXED set of causes (two runs, two endings, one locale) and an UNKNOWN one (a flag
    written before this round carries no `cause`) both fall back to the neutral clause,
    which is true of every ending. ASCII only, no em dashes -- see the caller."""
    from app.services import quality

    known = {c for c in causes if c}
    if len(known) == 1 and len(causes) == 1:
        cause = known.pop()
        if cause == quality.GATE_HOLD_CAUSE_JUDGE_UNAVAILABLE:
            return (
                f"were produced by a run whose check {named} could NOT BE EVALUATED (the "
                "judge was configured but returned no verdict), so the text was never "
                "judged at all"
            )
        if cause == quality.GATE_HOLD_CAUSE_UNREPAIRED:
            return (
                f"were produced by a run that ENDED with {named} still FAILING and no "
                "correction loop to repair the text"
            )
        if cause == quality.GATE_HOLD_CAUSE_LOOP_EXHAUSTED:
            return (
                f"were produced by a run whose correction loop RAN OUT OF VISITS with "
                f"{named} still FAILING"
            )
        if cause == quality.GATE_HOLD_CAUSE_NO_PROGRESS:
            return (
                f"were produced by a run whose correction loop STOPPED MAKING PROGRESS "
                f"(the repair returned text {named} had already judged) with that check "
                "still FAILING"
            )
        if cause == quality.GATE_HOLD_CAUSE_VISIT_CAP:
            return (
                f"were produced by a run that stopped at {named}'s own max-visits ceiling "
                "with the check still FAILING"
            )
    return f"were produced by a run whose check {named} did not pass them"


def _gate_held_reason_for_docs(session: Session, doc_ids, locale: str, *, unit: str) -> str | None:
    """The shared scan behind both gate-hold predicates -- ONE rule, two delivery modes."""
    # Lazy import: `quality` pulls the tagging/QA rungs, and this module is imported by
    # every delivery surface -- keep the module-level import graph exactly as it was.
    from app.services import quality

    rows = session.exec(
        select(Segment.status, Segment.qa_flags).where(
            Segment.document_id.in_(list(doc_ids)), Segment.target_locale == locale
        )
    ).all()
    steps: list[str] = []
    causes: set[str] = set()
    n = 0
    for status, flags in rows:
        if status in (SegStatus.APPROVED, SegStatus.EDITED):
            # A human has already reviewed this unit -- that IS the approval this hold
            # waits for, so it no longer holds even if the badge is still on the row.
            continue
        for f in flags or []:
            if isinstance(f, dict) and f.get("type") == quality.GATE_HOLD_FLAG_TYPE:
                n += 1
                st = str(f.get("step") or "").strip()
                if st and st not in steps:
                    steps.append(st)
                causes.add(str(f.get("cause") or ""))
                break
    if not n:
        return None
    named = ", ".join(repr(x) for x in steps[:3]) or "an LQA check"
    return (
        f"locale {locale} is held by a quality gate: {n} {unit}(s) "
        f"{_gate_held_cause_clause(causes, named)}. Review (approve or edit) them "
        # ASCII ONLY (issue #15 + gate C round 2, L1): this string is raised VERBATIM as a
        # CliError body (`doc export` -> "export refused: <this>"), and program chrome is
        # pure ASCII whatever the console code page. It is also outward-facing copy, so
        # The maintainer's standing no-em-dash rule applies. `verify_cli_ascii`'s fourth check now
        # follows CLI chrome back into the service functions that build it.
        "before delivering. This hold is not bypassable with allow_held."
    )


def assert_deliverable(session: Session, doc: Document, locale: str) -> None:
    """Raise ExportHeldLocale when `locale` is held for `doc` (see held_locale_reason).
    The single held-predicate seam every delivery surface consults before
    build_translated_file: web + /api document export, `doc export`, MCP, the project
    bundle (delivery.export_project), AND the connector attachment path
    (jira._locale_is_held) all refuse/skip a held locale identically. (Each surface calls
    held_locale_reason directly for a skip-and-report or a way-out-bearing 409; this
    raising twin is for the callers that want an exception, e.g. the single-file bundle
    fast path.)"""
    reason = held_locale_reason(session, doc, locale)
    if reason is not None:
        raise ExportHeldLocale(reason, doc_id=doc.id, locale=locale)
