"""FULL_TEXT per-paragraph view + recovery (GitHub #42).

THE PROBLEM. In `TransMode.FULL_TEXT` the segment IS the whole document, so the CAT
editor offers one giant unit. When a fix step drops the inline-formatting tokens out of
ONE paragraph, `export.paragraph_texts_for`'s guard B refuses the export — correctly,
because shipping it would silently collapse that paragraph's formatting. But there was
no way back out: `tag_repair.repair()` bails by contract on a paired-token source, the
LLM rung is off by default, and nothing in the product told the linguist WHICH tokens
diverged (the editor renders `⟦r1⟧` as a numbered chip, which is on no keyboard and
cannot be copied). Measured on real data — doc 44, `AcmeGamesMarketing.docx`, `es-419`,
six dropped tokens all in paragraph 0 of 5 — the document was permanently unexportable.

THE SEAM. Every surface (editor page, `/api`, `loc-tms doc paragraphs` /
`revert-paragraph` / `set-paragraph`) is a thin caller of the functions here. That is
deliberate and not negotiable: `tagging verify`'s logic is written inline in
`cmd_tagging.py` with NO web twin, and that parity debt is exactly what this module
must not repeat.

CONSERVATIVE QUARANTINE SEMANTICS (advisory dropped, GitHub #42). ANY tag divergence is
damage — identical to `quality.tag_damage` (non-empty ⇒ damaged). A shape-preserving
reorder counts as damage: shown red, offered a revert, quarantined by export guard B.
The recovery surface is the mitigation. There is no loss-free / advisory classification
anywhere in this module.

WHY THE SPLIT IS EXACT AND FREE. `ingest.py` builds the FULL_TEXT source as
`"\\n\\n".join(unit_texts)`, so the split yields exactly `len(units)` parts and every
part equals its unit text. Blank "\\n\\n" split artifacts (a doubled delimiter, a leading/
trailing blank line) are absorbed by the shared `align_parts` predicate — non-blank parts
are matched to non-blank units, so those shapes round-trip instead of reading as a desync
(finding 4). When the REAL count does not align (a merged delimiter that fuses two
paragraphs, or a dropped/added real paragraph) there is no trustworthy per-paragraph view
at all — one merged blank line shifts EVERY later paragraph — so `split_view` refuses and
neither writer will run. Guard A keeps its exclusive claim on the global quarantine.

THE LAUNDERING TRAP, which is the whole reason this module is not three lines. A naive
splice — replace `parts[i]`, join, commit — produces a body that is structurally
perfect: `tag_damage` returns "", the segment commits `status=mt`, `qa_flags=None`,
`tm_eligible=True`, and it becomes connector-pushable while 21% of it is still English,
with nothing recording why. That is a REGRESSION dressed as a recovery. So
`revert_paragraph` writes four properties, asserted on the COMMITTED row from a fresh
Session in `tests/verify_fulltext_recovery.py`: NEEDS_REVIEW, a durable `source_fallback`
qa_flag, `tm_eligible=False` plus an ENFORCED refusal in `tm.write_back`/`tm.ft_store`,
and an audit row.

Both writers splice and commit the WHOLE body through `quality.commit_mt_output`, the
sole sanctioned `target_text` writer, so every word-level verdict (refusal, DNT,
glossary, tag integrity) is recomputed on the real delivered text.

Documented in: docs/architecture/translation-pipeline.md
"""

import logging
import unicodedata

from sqlmodel import Session, select

from app.models import Document, Segment, SegStatus, TransMode
from app.services import audit, postedit, qe, quality

_LOG = logging.getLogger(__name__)


# --- ONE classification of every outcome these functions return ---
# The writers/readers below return `{"ok": ...,"reason": ...}` dicts, and each surface
# used to decide for itself what kind of event that was. They disagreed: POST
# /documents/{id}/paragraphs/{i}/save answered 200 `{"ok": false, "needs_confirm":
# true}` while its /api twin answered 409 for the SAME refusal. Two surfaces of one seam
# reporting one event differently is the parity debt this module exists not to repeat,
# so the classification lives HERE, once, and each transport keeps only its own idiom
# for RENDERING it.
#
#   "ok"          — it happened.
#   "confirmable" — REFUSED but re-submittable as-is with confirm=1. A blocking
#                   confirmation, not an error: the operator may fix the tokens instead.
#                   Nothing was written.
#   "conflict"    — the request is well-formed; the DOCUMENT's state makes it impossible
#                   (guard A, a SEGMENTED doc, a withheld write, an undamaged paragraph,
#                   the share ceiling).
#   "not_found"   — no such segment / paragraph.
#   "invalid"     — the SUBMITTED text is not acceptable (a blank line would add a
#                   paragraph and desync the positional map).
_OUTCOME_CLASS = {
    "tag_damage": "confirmable",
    "not_full_text": "conflict",
    "count_desync": "conflict",
    "write_refused": "conflict",
    "not_damaged": "conflict",
    "share_ceiling": "conflict",
    "awaiting_review": "conflict",
    "no_segment": "not_found",
    "bad_index": "not_found",
    "blank_line": "invalid",
}


def _held_for_review(session: Session, doc: Document, locale: str) -> dict | None:
    """#617 item 4 (gate): the refusal for a document+locale a human_review pause holds (409
    on web and /api via the "conflict" class, a CLI error), or None."""
    from app.services import segment_review

    try:
        segment_review.refuse_if_awaiting_review(session, doc.id, locale)
    except segment_review.AwaitingReviewError as exc:
        return {"ok": False, "reason": exc.code, "run_id": exc.run_id, "message": str(exc)}
    return None


def _is_invisible(ch: str) -> bool:
    """True for a character that carries no visible glyph, so appending it cannot make a
    string a real translation of its source. CATEGORY-based, not a denylist (review
    round 2): the first fix hardcoded six chars, and U+2064 INVISIBLE PLUS / U+FE0F /
    U+180E / U+3164 (and others) laundered the source_fallback flag right past it. Unicode
    category `Cf` (format) covers the zero-width space/joiner family, the word joiner, the
    BOM, the soft hyphen and the invisible-operator block U+2060-2064; the explicit ranges
    add the variation selectors and the Mongolian/Hangul fillers, which are not `Cf`.
    Persian/Indic ZWNJ/ZWJ ARE stripped here, but that only affects the SOURCE-EQUALITY
    check — a genuine translation differs from the English source by real words, so it is
    never mistaken for source; a probe confirms that case."""
    o = ord(ch)
    return (
        unicodedata.category(ch) == "Cf"
        or 0xFE00 <= o <= 0xFE0F  # variation selectors
        or 0xE0100 <= o <= 0xE01EF  # variation selectors supplement
        or o in (0x180E, 0x115F, 0x1160, 0x3164, 0xFFA0)
    )  # Mongolian/Hangul fillers


def _norm_cmp(s: str | None) -> str:
    """`s` with invisible/format chars removed and surrounding whitespace stripped —
    the normalization the source-equality guard compares on."""
    return "".join(c for c in (s or "") if not _is_invisible(c)).strip()


def outcome_class(res: dict | None) -> str:
    """The semantic class of a `split_view` / `revert_paragraph` / `save_paragraph`
    result. Unknown reasons fall to "invalid" — a new refusal must never be reported as
    a success by a surface that has not been taught about it."""
    if (res or {}).get("ok"):
        return "ok"
    return _OUTCOME_CLASS.get((res or {}).get("reason"), "invalid")


def fallback_share(units, indices, source_text: str | None) -> float:
    """The share of the document BODY covered by paragraphs `indices`.

    One definition, used by `split_view`, both writers and `export`'s ceiling, so the
    number a reviewer is shown is the same number the ceiling is checked against.
    Character-based against the segment's own source text (doc 44: 313/1526 = 21%)."""
    total = len(source_text or "")
    if not total:
        return 0.0
    used = 0
    for i in indices or []:
        if 0 <= i < len(units):
            used += len((units[i] or {}).get("text") or "")
    return used / total


def _load(session: Session, doc: Document, locale: str):
    """(units, segment) for a FULL_TEXT doc+locale, or (None, None)."""
    if doc is None or doc.mode != TransMode.FULL_TEXT:
        return None, None
    units = list((doc.skeleton or {}).get("units", []))
    seg = session.exec(
        select(Segment)
        .where(Segment.document_id == doc.id, Segment.target_locale == locale)
        .order_by(Segment.order_index)
    ).first()
    return units, seg


def _real_parts(full: str) -> list[str]:
    """The body's REAL paragraphs: the "\\n\\n" split with blank artifacts dropped.

    Round-1 LOW-2: ONE definition of "a real paragraph" on the TARGET side, so `align_parts`'
    verdict and the counts `count_desync` prints (and the run warning shows the operator) can
    never drift apart -- change what a paragraph is here and both move together."""
    return [p for p in (full or "").split("\n\n") if p.strip()]


def _real_units(units: list) -> int:
    """The skeleton's REAL paragraph count -- the SOURCE-side half of the same definition
    (a whitespace-only unit is not a paragraph). See `_real_parts` (round-1 LOW-2)."""
    return sum(1 for u in units or [] if ((u or {}).get("text") or "").strip())


def paragraph_counts(full: str, units: list) -> tuple[int, int]:
    """`(real paragraphs in the body, real paragraphs in the skeleton)` -- the two numbers
    `align_parts` compares to reach its verdict, exposed so a caller that PRINTS them prints
    exactly what the verdict was computed from (round-1 LOW-2)."""
    return len(_real_parts(full)), _real_units(units)


def align_parts(full: str, units: list) -> tuple[list[str] | None, str]:
    """Map a FULL_TEXT target BODY onto its skeleton `units`, absorbing blank "\\n\\n"
    split artifacts. Callers handle their own blank-body MESSAGING first (an empty target
    is "untranslated" for the reader, a blank STORED target is a bug for the exporter), but
    a blank `full` is TOLERATED here: with no non-blank parts and no non-blank units it
    returns an all-blank aligned list (the export all-units-blank / degenerate caller
    passes one) rather than a desync.

    ONE DEFINITION, shared by `_parts` (this module: split_view + the revert/save writers)
    and `export.paragraph_texts_for`, so the per-paragraph view, the writers and the
    deliverable can never disagree on what counts as a desync.

    Returns `(aligned, reason)`:
      * `aligned` — a list of exactly `len(units)` target strings: each NON-BLANK unit
        consumes the next non-blank part in order; a WHITESPACE-ONLY unit maps to its own
        (blank) source text and consumes no part. `None` on a desync.
      * `reason` — "" on success, "under" (fewer real translated paragraphs than real
        source paragraphs) or "over" (more) on a desync.

    The comparison is SYMMETRIC — non-blank PARTS against non-blank UNITS — so a legacy
    doc whose SOURCE itself carries a blank unit (an empty XLIFF <source/>, a whitespace
    PO msgid) round-trips instead of being permanently unexportable. A blank TRANSLATION
    of a real (non-blank) paragraph, e.g. an NBSP-only output, therefore drops a real part
    and reads as a desync ("under") — a blank paragraph is not a translation. Indices are
    NOT renumbered: `aligned[i]` is unit `i`'s target, exactly the index operators see in
    split_view and the writers splice on."""
    non_blank_parts = _real_parts(full)
    nb_units = _real_units(units)
    if len(non_blank_parts) < nb_units:
        return None, "under"
    if len(non_blank_parts) > nb_units:
        return None, "over"
    it = iter(non_blank_parts)
    aligned = [
        next(it) if ((u or {}).get("text") or "").strip() else ((u or {}).get("text") or "")
        for u in units
    ]
    return aligned, ""


def count_desync(full: str, units: list) -> dict | None:
    """The paragraph-count verdict on a STORED FULL_TEXT body, as data (issue #107).

    `None` when the body aligns with the skeleton; else a dict with `desync` (over / under --
    `align_parts`' own verdict, NOT one of this module's surface `reason` codes, which is why
    the key is named differently) plus `parts` and `units`: the two counts the exporter's
    quarantine message prints, so a caller can say exactly the same thing.

    Computed through the SAME `align_parts` predicate the export guard, `split_view` and the
    paragraph writers use, and the two NUMBERS come from `paragraph_counts`, the same helper
    that predicate compares (round-1 LOW-2) -- so nothing can drift: this is a READ of the
    shared definition, not a second one. A blank body is not this predicate's business (the
    the exporter's own blank-body guard) and returns None -- the caller decides whether an
    empty body is interesting.

    WHY IT EXISTS: the count comparison only ever ran at EXPORT time, so a FULL_TEXT run whose
    model split 111 source paragraphs into 120 reported `completed` with `warnings: []`, and
    the operator learned the body was unusable 30 minutes later, at the download. The orchestrator
    runs it at write-back so the RUN is honest about what it produced."""
    if not (full or "").strip() or not units:
        return None
    aligned, reason = align_parts(full, units)
    if aligned is not None or not reason:
        return None
    parts, nb_units = paragraph_counts(full, units)
    return {"desync": reason, "parts": parts, "units": nb_units}


def _parts(seg: Segment, units: list) -> list[str] | None:
    """The target body split into a unit-indexed list, or None on a real count desync.

    An EMPTY target is not a desync — the document is simply untranslated, so every
    paragraph reads as blank and nothing is damaged.

    When the raw split ALREADY has one part per unit, it is used POSITIONALLY (unchanged):
    a paragraph an operator CLEARED to blank is a legitimate intermediate edit state that
    must keep its slot — this is an EDITING view, not the deliverable (export separately
    refuses a blank target of a real source paragraph; the two views legitimately differ
    on delivery-readiness, they agree on STRUCTURE). Only when the raw count does NOT match
    do we absorb blank "\\n\\n" artifacts (a doubled delimiter, a leading/trailing blank
    line) via the shared `align_parts` predicate — the finding-4 fix, so split_view + the
    revert/save writers stop refusing shapes export handles. Either way the result is
    unit-indexed (len == len(units)), so `parts[index]` in the writers still addresses the
    SAME paragraph the operator saw — no renumbering."""
    full = seg.target_text or ""
    if not full.strip():
        return ["" for _ in units]
    raw = full.split("\n\n")
    if len(raw) == len(units):
        return raw
    aligned, _reason = align_parts(full, units)  # absorb blank artifacts; None on a desync
    return aligned


def _rows(units: list, parts: list[str], *, untranslated: bool = False) -> list[dict]:
    """One row per paragraph, judged by the SAME predicate the commit gate and the
    export guard use.

    `quality.tag_damage_detail` — not a token multiset of our own. This loop used to
    compare `Counter(find_placeholders(...))`, which is a strict subset of `tag_damage`
    and therefore blind to reordering, broken nesting, a residual sentinel, and an
    emptied paired span. `reason` is the authoritative field — missing/extra are both
    empty whenever the damage preserved the multiset.

    `damaged` is `True` iff ANYTHING diverged (conservative semantics). It is exactly
    `export`'s guard-B predicate, so this view cannot disagree with the export beside
    it."""
    rows = []
    for idx, u in enumerate(units):
        src, tgt = u.get("text") or "", parts[idx]
        # An UNTRANSLATED document is not damaged, it is empty. Every tokenised source
        # paragraph would otherwise report its whole token set as "missing" and offer a
        # revert-to-source that changes nothing — export already falls a blank target
        # back to its source, and this view must agree with export or it is lying.
        delta = None if untranslated else quality.tag_damage_detail(src, tgt)
        rows.append(
            {
                "index": idx,
                "p_index": u.get("p_index", idx),
                "source": src,
                "target": tgt,
                "reason": delta["reason"] if delta else "",
                "missing": delta["missing"] if delta else [],
                "extra": delta["extra"] if delta else [],
                "damaged": delta is not None,
            }
        )
    return rows


def split_view(session: Session, doc: Document, locale: str) -> dict:
    """Per-paragraph source/target view for a FULL_TEXT document.

    Returns `{"ok": True, "rows": [{index, p_index, source, target, reason, missing,
    extra, damaged}], "damaged_count", "damaged_indices", "damaged_share",
    "reverted_indices", "fallback_share", ...}`.

    `damaged` is `quality.tag_damage_detail`'s verdict — the SAME predicate the commit
    gate and the export guard use — and `reason` is its string. Render `reason`, not
    just missing/extra: the multiset-preserving damage classes (emptied span, malformed
    nesting) have an EMPTY delta on both sides.

    `damaged_share` is what a revert-everything WOULD substitute; `fallback_share` is
    what ALREADY is source text (read from the committed `source_fallback` flag, so it
    survives a reload and is the same number the CLI and /api report).

    Refuses with `{"ok": False, "reason": ...}`:
      `not_full_text`  — a SEGMENTED document is untouched by every part of #42;
      `no_segment`     — no segment for that locale;
      `count_desync`   — guard A. The REAL (non-blank) paragraph count does not align
                         even after blank "\\n\\n" artifacts are absorbed, so the positional
                         map is scrambled for every later paragraph and NO per-paragraph
                         view is reliable. No rows, and neither writer will run."""
    # Every refusal carries a `message`. The in-process CLI arm renders these from
    # `reason`, but the --remote arm only ever sees the HTTP body, so the seam owns the
    # sentence; each transport still chooses how to show it.
    units, seg = _load(session, doc, locale)
    if units is None:
        return {
            "ok": False,
            "reason": "not_full_text",
            "message": "this document is not mode=full_text; the per-paragraph "
            "view applies only to a FULL_TEXT document.",
        }
    if seg is None:
        return {"ok": False, "reason": "no_segment", "message": "no segment for that locale."}
    parts = _parts(seg, units)
    if parts is None:
        n_parts = len((seg.target_text or "").split("\n\n"))
        return {
            "ok": False,
            "reason": "count_desync",
            "parts": n_parts,
            "units": len(units),
            "message": (
                "paragraph COUNT desync: the translated body has %d "  # noqa: UP031
                "paragraph(s), the source has %d. The positional map is "
                "scrambled for every later paragraph, so there is no "
                "reliable per-paragraph view and no revert is offered. "
                "Restore the blank lines between paragraphs first." % (n_parts, len(units))
            ),
        }
    untranslated = not (seg.target_text or "").strip()
    rows = _rows(units, parts, untranslated=untranslated)
    damaged = [r["index"] for r in rows if r["damaged"]]
    reverted = quality.source_fallback_indices(seg.qa_flags)
    return {
        "ok": True,
        "doc_id": doc.id,
        "locale": locale,
        "segment_id": seg.id,
        "untranslated": untranslated,
        "rows": rows,
        "damaged_count": len(damaged),
        "damaged_indices": damaged,
        "damaged_share": fallback_share(units, damaged, seg.source_text),
        "reverted_indices": reverted,
        "fallback_share": fallback_share(units, reverted, seg.source_text),
    }


def _commit_body(
    session: Session,
    seg: Segment,
    parts: list[str],
    *,
    provenance: str,
    pe_capture: bool = False,
    pe_actor: str = "",
):
    """Splice + commit the WHOLE body through the sole sanctioned target_text writer.

    `human_edit=True`: both callers (save_paragraph, revert_paragraph) are a PERSON's own
    edit of a full-text body, routed through the seam only for its tag-integrity gate. The
    seam's status-based human-work protection (which stops MACHINE overwrites of approved/
    edited work) must NOT block a human from post-editing their own body -- otherwise editing
    one paragraph (which sets the body EDITED) would refuse the NEXT paragraph edit, and a
    person could never re-edit an approved full-text doc (the CAT editor lets them re-edit an
    approved SEGMENT freely). The hard-lock gate still applies (a hard TM lock refuses a
    para-edit, unchanged).

    CAVEAT (issue #50, PRE-EXISTING): the save/revert routes that reach this seam gate on VIEW
    access, not write access (the whole document-write family does, segment_save included), so
    a viewer-scoped caller reaching them can overwrite an approved body through this carve-out
    -- the same exposure #50 already grants for non-approved content. That is a route-RBAC gap
    to close in #50, independent of this human-work protection.

    EMPTY-BODY NORMALIZATION (LOW-2, reviewer): joining an all-cleared body yields joiner
    residue ("\\n\\n" for a 2-paragraph doc), not "". Store "" EXACTLY when the whole body is
    whitespace-only, so a cleared body is falsy -- `export.build_translated_file`'s
    `s.target_text or s.source_text` must fall back to source, never emit blank lines -- and it
    matches the segmented editor's stored empty target. Done INSIDE the seam so target_text is
    never written outside `commit_mt_output`. A partial clear (one paragraph of many) leaves the
    body non-empty and is untouched, so the positional map is preserved."""
    body = "\n\n".join(parts)
    if not body.strip():
        body = ""
    # Post-edit capture seam 3 (SPEC-wave2 §3.2.4). Snapshot the pre-write state BEFORE
    # the seam overwrites the body — `classify_baseline` needs the target's origin. Only
    # save_paragraph passes pe_capture=True; revert_paragraph does NOT (a revert is a
    # discard, not post-editing effort — the stale episode self-corrects on the next
    # captured edit via final_sha, §3.2.4). A full-text body is a WHOLE-DOCUMENT unit, so
    # unit_kind='full_text' and the baseline is the entire body before the edit.
    _pe_pre = seg.target_text or ""
    _pe_pre_status = seg.status
    _pe_pre_tm_pct = seg.tm_match_pct
    _pe_pre_lock_band = seg.lock_band
    outcome = quality.commit_mt_output(
        session,
        seg,
        body,
        source=provenance,
        tm_eligible=False,
        set_status=False,
        human_edit=True,
        # reviewer H1: a full-text body is committed verbatim
        # (paragraphs join on "\n\n"), so exclude it from the
        # whitespace seam gate (a trailing/interior newline is
        # structural, not a defect). lang_guard is off here
        # anyway (human edit), so this is belt-and-suspenders.
        full_text=True,
    )
    if pe_capture and getattr(outcome, "committed", False):
        # MID-TRANSACTION (save_paragraph commits after this returns), so capture_guarded's
        # SAVEPOINT keeps a capture failure from poisoning the paragraph write. No commit.
        postedit.capture_guarded(
            session,
            segment=seg,
            pre=_pe_pre,
            post=seg.target_text or "",
            actor_kind="human",
            actor=pe_actor,
            unit_kind="full_text",
            target_locale=seg.target_locale,
            pre_status=_pe_pre_status,
            tm_match_pct=_pe_pre_tm_pct,
            lock_band=_pe_pre_lock_band,
        )
    return outcome


def revert_paragraph(
    session: Session, doc: Document, locale: str, index: int, *, actor: str, ip: str | None = None
) -> dict:
    """Replace paragraph `index` of the translated body with its SOURCE text.

    The recovery of last resort for a guard-B desync: the source paragraph's tokens are
    correct BY CONSTRUCTION, so the document exports again and keeps its formatting, and
    that paragraph is VISIBLY untranslated rather than invisibly de-formatted.

    Refuses (`{"ok": False, "reason": ...}`) for `not_full_text` / `no_segment` /
    `count_desync` (guard A owns that case), `bad_index`, `not_damaged` — the recovery
    path may not be used to quietly untranslate a paragraph that is fine — and
    `share_ceiling`, the SAME `export_source_fallback_max_share` limit `export`'s C-3
    override enforces (this route had none, and `--all-damaged` loops it, so reverting
    one paragraph at a time produced the very artifact the sanctioned door refuses).

    The four non-laundering invariants it WRITES (asserted on the committed row, not
    here): NEEDS_REVIEW, a `source_fallback` qa_flag naming every reverted index,
    `tm_eligible=False` on the commit (backed by the refusal in `tm.write_back` /
    `tm.ft_store`), and a `document.paragraph_revert` audit row carrying the share."""
    view = split_view(session, doc, locale)
    if not view["ok"]:
        return view
    held = _held_for_review(session, doc, locale)
    if held:
        return held
    units, seg = _load(session, doc, locale)
    staged = _stage_revert(session, doc, locale, index, units, seg)
    if not staged["ok"]:
        # A staged refusal wrote NOTHING that may survive: roll the transaction back so
        # a withheld splice (write_refused) leaves no half-applied state on the row.
        session.rollback()
        return staged
    session.commit()
    _audit_reverts(doc, locale, [index], staged["fallback_share"], actor, ip)
    _LOG.warning(
        "fulltext recovery: paragraph %d reverted to source (doc=%s locale=%s "
        "indices=%s share=%.3f)",
        index,
        doc.id,
        locale,
        staged["indices"],
        staged["fallback_share"],
    )
    after = split_view(session, doc, locale)
    return {
        "ok": True,
        "index": index,
        "indices": staged["indices"],
        "fallback_share": staged["fallback_share"],
        "damaged_count": after["damaged_count"] if after["ok"] else None,
        "damaged_indices": after["damaged_indices"] if after["ok"] else [],
        "rows": after["rows"] if after["ok"] else [],
    }


def _stage_revert(
    session: Session, doc: Document, locale: str, index: int, units: list, seg: Segment
) -> dict:
    """STAGE one paragraph revert (splice + status + qa_flags) via add()/flush() but
    DO NOT COMMIT and DO NOT AUDIT — the caller owns the transaction and the audit.

    This is the atomicity seam (review HIGH-1 / F-3). `revert_paragraph` calls it once
    then commits; `revert_all_damaged` calls it PER INDEX with no intervening commit and
    commits ONCE only after all succeed, so a mid-batch refusal (the ceiling door OR the
    `write_refused` refusal door) rolls the WHOLE batch back with no partial write. The
    caller assumes `split_view` already validated the document (guard A / full_text /
    segment); this function makes the per-index decisions (`bad_index`, `not_damaged`,
    `share_ceiling`, `write_refused`) and, when it stages, reads `seg.target_text` as it
    stands NOW — so a batch's later indices see the earlier ones already applied."""
    if not (0 <= index < len(units)):
        return {
            "ok": False,
            "reason": "bad_index",
            "index": index,
            "units": len(units),
            "message": (
                "paragraph index %d is out of range (the document has %d)." % (index, len(units))  # noqa: UP031
            ),
        }
    parts = _parts(seg, units)
    if parts is None:
        # A count desync appeared under us (guard A) — refuse rather than write blindly.
        return {
            "ok": False,
            "reason": "count_desync",
            "index": index,
            "message": "paragraph count desync; no per-paragraph revert is possible.",
        }
    already = quality.source_fallback_indices(seg.qa_flags)
    # An UNTRANSLATED document is EMPTY, not damaged — mirror `_rows`/`split_view`, or a
    # blank target would read as "missing every token" and offer a revert that changes
    # nothing (and disagree with export, which already falls a blank target to source).
    untranslated = not (seg.target_text or "").strip()
    delta = (
        None
        if untranslated
        else quality.tag_damage_detail(units[index].get("text") or "", parts[index])
    )
    if delta is None and index not in already:
        return {
            "ok": False,
            "reason": "not_damaged",
            "index": index,
            "message": (
                "paragraph %d is not export-blocking damaged, so it may "  # noqa: UP031
                "not be replaced with SOURCE text." % index
            ),
        }
    # THE SAME CEILING THE SANCTIONED DOOR ENFORCES, checked on the PROSPECTIVE set and
    # BEFORE anything is written, so a refusal leaves the row untouched.
    prospective = sorted(set(already) | {index})
    share = fallback_share(units, prospective, seg.source_text)
    from app.services.export import _source_fallback_ceiling

    ceiling = _source_fallback_ceiling()
    if share > ceiling:
        return {
            "ok": False,
            "reason": "share_ceiling",
            "index": index,
            "indices": prospective,
            "fallback_share": share,
            "ceiling": ceiling,
            "message": (
                "reverting paragraph %d would leave %.0f%% of the document "  # noqa: UP031
                "as untranslated SOURCE text, above the "
                "export_source_fallback_max_share ceiling of %.2f - the "
                "export would be refused anyway. Repair the tokens instead "
                "(doc set-paragraph / the editor)." % (index, share * 100.0, ceiling)
            ),
        }
    parts[index] = units[index].get("text") or ""
    outcome = _commit_body(session, seg, parts, provenance="para-source-fallback")
    if not outcome.committed:
        # The choke point withheld the write (a refusal verdict on the spliced body).
        # Leave the (uncommitted) refusal state for the CALLER to roll back — in a batch
        # this rollback also undoes every EARLIER staged index, which is the whole point.
        return {
            "ok": False,
            "reason": "write_refused",
            "index": index,
            "message": (
                "the write seam withheld the spliced body (a refusal verdict); nothing was written."
            ),
        }
    # commit_mt_output leaves status alone (set_status=False) by design, so the review
    # routing is set HERE, explicitly. A recovered document is NOT done. flush() lands
    # the change in the open transaction (so a later staged index sees it) WITHOUT
    # committing, so a subsequent refusal can still roll it all back.
    seg.status = SegStatus.NEEDS_REVIEW
    seg.qa_flags = quality.merge_source_fallback_flag(seg.qa_flags, prospective)
    session.add(seg)
    session.flush()
    return {"ok": True, "index": index, "indices": prospective, "fallback_share": share}


def _audit_reverts(
    doc: Document, locale: str, indices, share: float, actor: str, ip: str | None
) -> None:
    """One `document.paragraph_revert` audit row per reverted index, written AFTER the
    data commit (audit.log uses its own session/transaction, so a rolled-back batch must
    leave no audit rows — hence audit only once the commit has succeeded)."""
    idx_str = ",".join(str(i) for i in sorted(set(indices)))
    for i in indices:
        audit.log(
            actor=actor,
            action="document.paragraph_revert",
            target="doc %s %s p%d" % (doc.id, locale, i),  # noqa: UP031
            detail=f"share={share:.3f} indices={idx_str}",
            ip=ip or "",
            customer_id=audit.customer_for_project(doc.project_id),
        )


def revert_all_damaged(
    session: Session, doc: Document, locale: str, *, actor: str, ip: str | None = None
) -> dict:
    """Atomically revert EVERY currently-damaged paragraph (the `--all-damaged` batch).

    GENUINELY ATOMIC (review HIGH-1 / F-3). The first cut looped the COMMITTING
    `revert_paragraph`, so a mid-batch refusal — whether the share ceiling OR the
    `write_refused` refusal door (a paragraph whose SOURCE text trips `is_refusal` on the
    spliced body) — left the earlier indices durably committed while the caller was told
    "nothing was written". Now every index is STAGED through `_stage_revert` (add()/
    flush(), no commit); the transaction is committed ONCE only after ALL succeed, and any
    failure rolls the WHOLE transaction back, so no partial revert can survive by either
    door. The up-front ceiling pre-check stays (a fast, whole-batch refusal), but the
    per-index staging is what makes the refusal-door case atomic too.

    `fallback_share` is monotonic in the index set, so the whole batch's share is the
    share of reverting ALL targets. Returns the same reason shapes `revert_paragraph`
    does so every surface classifies it identically."""
    view = split_view(session, doc, locale)
    if not view["ok"]:
        return view
    held = _held_for_review(session, doc, locale)
    if held:
        return held
    targets = list(view["damaged_indices"])
    if not targets:
        return {
            "ok": False,
            "reason": "not_damaged",
            "indices": [],
            "message": "no damaged paragraph to revert.",
        }
    units, seg = _load(session, doc, locale)
    already = quality.source_fallback_indices(seg.qa_flags)
    prospective = sorted(set(already) | set(targets))
    share = fallback_share(units, prospective, seg.source_text)
    from app.services.export import _source_fallback_ceiling

    ceiling = _source_fallback_ceiling()
    if share > ceiling:
        # A fast whole-batch refusal, having STAGED nothing.
        return {
            "ok": False,
            "reason": "share_ceiling",
            "indices": prospective,
            "fallback_share": share,
            "ceiling": ceiling,
            "message": (
                "reverting all %d damaged paragraph(s) would leave %.0f%% of "  # noqa: UP031
                "the document as untranslated SOURCE text, above the "
                "export_source_fallback_max_share ceiling of %.2f - the "
                "export would be refused anyway. Repair the tokens instead "
                "(doc set-paragraph / the editor). Nothing was reverted."
                % (len(targets), share * 100.0, ceiling)
            ),
        }
    for i in targets:
        staged = _stage_revert(session, doc, locale, i, units, seg)
        if not staged["ok"]:
            # Roll the WHOLE transaction back — this undoes every EARLIER staged index
            # (none has been committed), so no partial revert survives.
            session.rollback()
            return {
                "ok": False,
                "reason": staged.get("reason", "write_refused"),
                "index": i,
                "indices": targets,
                "message": (
                    "batch revert aborted at paragraph %d; %s. Nothing was "  # noqa: UP031
                    "reverted." % (i, staged.get("message") or staged.get("reason"))
                ),
            }
    session.commit()
    _audit_reverts(doc, locale, targets, share, actor, ip)
    _LOG.warning(
        "fulltext recovery: %d paragraph(s) reverted to source atomically "
        "(doc=%s locale=%s indices=%s share=%.3f)",
        len(targets),
        doc.id,
        locale,
        targets,
        share,
    )
    after = split_view(session, doc, locale)
    return {
        "ok": True,
        "indices": targets,
        "reverted": targets,
        "fallback_share": share,
        "damaged_count": after["damaged_count"] if after["ok"] else None,
        "damaged_indices": after["damaged_indices"] if after["ok"] else [],
        "rows": after["rows"] if after["ok"] else [],
    }


def save_paragraph(
    session: Session,
    doc: Document,
    locale: str,
    index: int,
    text: str,
    *,
    actor: str,
    ip: str | None = None,
    confirm: bool = False,
) -> dict:
    """Replace paragraph `index` with `text` — the hand repair the linguist actually
    wants (retype the `⟦rN⟧` tokens around the translated words).

    A save that would RE-BREAK the whole-body tag integrity is REFUSED with
    `{"ok": False, "reason": "tag_damage"}` and writes nothing, unless `confirm` — the
    same blocking-confirmation posture as the editor's `segment_save` guard, so this
    surface cannot be the soft one.

    On a clean save both stale badges are cleared: the `tag_integrity` flag (nothing
    cleared it before #42, so a repaired paragraph kept the badge forever) and this
    paragraph's `source_fallback` entry — the paragraph is no longer source text, and a
    flag that outlives its verdict is worse than no flag.

    THE BLOCK DECISION IS LOCAL, THE CLEARING IS OVER THE WHOLE DOCUMENT (the residual
    scan). The BLOCK is rightly local — a save is refused for what THIS save does — but
    the badge is a property of the SEGMENT, which in FULL_TEXT is the whole document. So
    a clean save may only clear the badge if NO OTHER paragraph is still damaged, judged
    by the same `_rows`/`tag_damage_detail` predicate over the CANDIDATE body. Token ids
    restart at 1 per paragraph and `segment.paired_span_bodies` keeps only the LAST body
    for a repeated name, so the paragraph-local verdict is strictly sharper than the
    whole-body one; both are consulted.

    STATUS on a clean save: EDITED (human work), UNLESS the save leaves the WHOLE body
    empty -- then NEW, mirroring the segmented editor's `not target_text.strip()` -> NEW so
    the documented "clear the target -> the machine re-translates it" escape hatch works on
    the full-text surface too (see the status block below for the whole-body-vs-paragraph
    rule)."""
    view = split_view(session, doc, locale)
    if not view["ok"]:
        return view
    held = _held_for_review(session, doc, locale)
    if held:
        return held
    units, seg = _load(session, doc, locale)
    if not (0 <= index < len(units)):
        return {
            "ok": False,
            "reason": "bad_index",
            "index": index,
            "units": len(units),
            "message": (
                "paragraph index %d is out of range (the document has %d)." % (index, len(units))  # noqa: UP031
            ),
        }
    if "\n\n" in (text or ""):
        # A blank line would ADD a paragraph and desync the positional map — exactly
        # the guard-A corruption this whole module refuses to create.
        return {
            "ok": False,
            "reason": "blank_line",
            "message": "a paragraph may not contain a blank line (it would add a "
            "paragraph and desync the positional map)",
        }
    parts = _parts(seg, units)
    parts[index] = text or ""
    candidate = "\n\n".join(parts)

    # PER-PARAGRAPH first, whole-body second, and BOTH. The paragraph verdict is the one
    # export enforces, and it is strictly sharper here: token ids restart at 1 in every
    # paragraph, so on the whole blob `segment.paired_span_bodies` keeps only the LAST
    # body for a repeated name and an emptied `⟦r2⟧⟦/r2⟧` in paragraph 0 hides behind a
    # healthy r2 in paragraph 3. Judging only the whole body would let this writer call a
    # save clean, clear the badge and set EDITED, while the split view beside it showed
    # "tags broken" and the export quarantined. The whole-body call stays as the backstop
    # for anything cross-paragraph.
    def _blocking_reason(src: str | None, tgt: str | None) -> str:
        d = quality.tag_damage_detail(src, tgt)
        return "" if d is None else d["reason"]

    # LOW-1 (reviewer): SKIP the tag-damage guard when the candidate WHOLE body is
    # whitespace-only -- that is a CLEAR, not a formatting break. Mirrors the segmented editor
    # (`documents.segment_save` runs its tag check only `if target_text.strip():`): an empty
    # body has no delivered text to lose formatting on (export falls it back to source), so
    # `tag_damage(source_para, "")` reporting "dropped tokens" and refusing the clear with
    # "formatting will be lost on export. Save anyway?" is wrong -- it blocked the documented
    # escape hatch on a TAGGED body. On a MULTI-paragraph body a single cleared paragraph leaves
    # the body non-empty, so the guard still runs (that paragraph's dropped tags DO cost
    # formatting in the still-delivered body, exactly as before).
    reason = ""
    if candidate.strip():
        reason = _blocking_reason(units[index].get("text") or "", parts[index]) or _blocking_reason(
            seg.source_text, candidate
        )
    if reason and not confirm:
        return {
            "ok": False,
            "reason": "tag_damage",
            "needs_confirm": True,
            "detail": reason,
            "message": f"This save breaks inline formatting tags ({reason}) - formatting "
            "will be lost on export. Save anyway?",
        }
    outcome = _commit_body(
        session, seg, parts, provenance="para-edit", pe_capture=True, pe_actor=actor
    )
    if not outcome.committed:
        session.rollback()
        return {
            "ok": False,
            "reason": "write_refused",
            "index": index,
            "message": (
                "the write seam withheld the spliced body (a refusal verdict); nothing was written."
            ),
        }
    # Clear this paragraph's source_fallback entry ONLY if the saved text actually
    # differs from the source. It used to drop the index unconditionally, and
    # `paragraphs.html` has a "Copy source" button directly above the textarea — so
    # copy-source + save was a two-click laundering flow: the paragraph is still English,
    # the durable "these paragraphs are UNTRANSLATED" flag is gone, the status reads
    # EDITED, and the export reports nothing. A save that re-states the source is not a
    # repair; it is the fallback again, by hand. Compared on a ZERO-WIDTH-NORMALIZED
    # strip (review MEDIUM/LOW): `.strip()` alone does not remove U+200B and friends, so
    # retyping the source with a trailing zero-width space read as "not source", cleared
    # the flag, flipped NEEDS_REVIEW->EDITED and shipped effectively-untranslated text
    # with NO substitution record. `_norm_cmp` strips the invisible format chars so an
    # invisible edit can no longer buy a clean flag.
    _still_source = _norm_cmp(text) == _norm_cmp(units[index].get("text"))
    indices = [
        i for i in quality.source_fallback_indices(seg.qa_flags) if i != index or _still_source
    ]
    seg.qa_flags = quality.merge_source_fallback_flag(seg.qa_flags, indices)
    # `outcome.tag_violation` is the seam's OWN post-write verdict on the spliced body —
    # read it rather than recomputing. It is non-empty only on a `confirm=True` save that
    # really did break the tags; in that case commit_mt_output has already merged a
    # tag_integrity flag and set NEEDS_REVIEW, and overwriting that with EDITED here would
    # hide a deliberate override behind a normal-looking status.
    #
    # `reason` is OR'd in because it can see a paragraph-local break the whole-body gate
    # cannot (the repeated-token-name collision above). When it fires alone the gate
    # merged no flag, so merge it here — a confirmed override that leaves NO badge is
    # exactly the silence #42 is about.
    #
    # `residual` is EVERY paragraph of the committed body, judged by the same predicate,
    # so a badge is never cleared while some OTHER paragraph is still damaged. Judged on
    # the COMMITTED body re-split from `seg.target_text`, NOT on the `parts` we handed in:
    # `commit_mt_output` may have REWRITTEN the text (deterministic tag repair, or the LLM
    # rung when on). A committed body that no longer splits into `len(units)` paragraphs
    # is guard A's case and cannot be judged per paragraph — treat it as unresolved.
    committed = _parts(seg, units)
    if committed is None:
        residual = [{"reason": "paragraph count desync after save"}]
    else:
        residual = [
            r
            for r in _rows(units, committed, untranslated=not "".join(committed).strip())
            if r["damaged"]
        ]
    gate_violation = bool(outcome.tag_violation)
    # WHOLE-BODY-EMPTY -> NEW, the documented "clear the target -> hand it back to the
    # machine" escape hatch (F1, live prod QA 2026-08-13). The segmented editor
    # (`documents.segment_save`) sets NEW the instant a saved target is blank
    # (`not target_text.strip()`, and it skips its tag-damage guard for an empty target);
    # this surface set EDITED UNCONDITIONALLY on a clean save, so clearing a full-text body
    # left it empty + EDITED. The human-work guard (`quality.commit_mt_output`) excludes
    # EDITED from EVERY machine call, so an empty + EDITED body was STRANDED: no rerun could
    # ever refill it and `export.held_locale_reason` held the locale forever (live repro:
    # doc 118 seg 3152). Mirror the segmented emptiness test EXACTLY (`not <target>.strip()`)
    # and let it take PRECEDENCE over the damage branches below: an empty body has NO
    # delivered text that could have broken formatting -- `tag_damage`/`outcome.tag_violation`
    # report every source token "dropped" on an empty target, but `_rows`/`split_view` and
    # `export` all treat an empty target as UNTRANSLATED (not damaged) and export falls it
    # back to source, so the honest status is NEW with no stale badges, mirroring what the
    # segmented editor records for a cleared segment (same NEW status; same tag/qe/char-limit
    # families cleared below). Not literally byte-for-byte identical: the FULL_TEXT path routes
    # through `commit_mt_output`, which also runs its number/dnt/glossary/scaffold/length gates
    # -- all no-ops on empty text -- and manages the `source_fallback` family the segmented
    # surface has no analogue for.
    #
    # WHOLE-BODY vs PER-PARAGRAPH (the unit-of-status question the fix turns on): the
    # SEGMENT -- the whole full-text body -- is the unit of status, so ONLY an entirely-empty
    # body goes NEW. Clearing ONE paragraph of a MULTI-paragraph body leaves the body
    # non-empty, so it stays EDITED below; that does NOT strand it -- the body as a whole is
    # still a human-editable unit (a person can retype that paragraph in the editor, the
    # human writers here bypass the guard via `human_edit=True`) and `export` still delivers
    # the non-empty body. Only the whole-body-empty case was stranded, and that is exactly
    # the case the live repro hit (doc 118 was a single-paragraph body).
    if not (seg.target_text or "").strip():
        # LOW-3 (reviewer): clear the SAME stale-badge families the segmented human save clears
        # (documents.segment_save: qe + tag_integrity + char_limit) -- a cleared body must not
        # keep a "QE 40/100, routed to review" or an over-limit badge describing text that no
        # longer exists. clear_tag_flags is LOAD-BEARING: on a TAGGED body `commit_mt_output`
        # sees the source tokens "dropped" from the empty target and MERGES a tag_integrity flag
        # (a tagless body it clears), so this drops it either way. char_limit was already cleared
        # by the seam (empty text never over-limit), so that call is idempotent. qe is NOT run at
        # this seam (QE is segmented-only), so clearing it here is the belt that catches any
        # pre-existing qe entry on the body segment.
        seg.qa_flags = quality.clear_tag_flags(seg.qa_flags)
        seg.qa_flags = qe.clear_qe_flag(seg.qa_flags)
        seg.qa_flags = quality.clear_char_limit_flags(seg.qa_flags)
        # reviewer H2b: clear the LC-7/QA-1 families too — a cleared body must not keep a
        # MAJOR "placeholder dropped" badge (placeholder_violation now abstains on empty
        # text, but an earlier commit's badge can still be present) or a stale whitespace one.
        seg.qa_flags = quality.clear_placeholder_flags(seg.qa_flags)
        seg.qa_flags = quality.clear_whitespace_flags(seg.qa_flags)
        seg.qa_flags = quality.clear_review_hold_flags(seg.qa_flags)
        seg.status = SegStatus.NEW
    elif gate_violation or reason or indices or residual:
        seg.status = SegStatus.NEEDS_REVIEW
        # Name what is ACTUALLY still broken. Prefer this save's own verdict (`reason`),
        # else the first residual paragraph — a badge that survives must say why.
        if not gate_violation:
            keep = reason or (residual[0]["reason"] if residual else "")
            if keep:
                seg.qa_flags = quality.merge_tag_flags(seg.qa_flags, keep)
            elif indices:
                # Only a source_fallback remains: the tag badge has nothing left to
                # describe, and leaving it would make the ONE thing that is true
                # (paragraphs are still English) compete with something that is not.
                seg.qa_flags = quality.clear_tag_flags(seg.qa_flags)
    else:
        seg.qa_flags = quality.clear_tag_flags(seg.qa_flags)
        seg.status = SegStatus.EDITED
    session.add(seg)
    session.commit()
    audit.log(
        actor=actor,
        action="document.paragraph_save",
        target="doc %s %s p%d" % (doc.id, locale, index),  # noqa: UP031
        detail="tag_damage=%s" % (reason or "none"),
        ip=ip or "",
        customer_id=audit.customer_for_project(doc.project_id),
    )
    after = split_view(session, doc, locale)
    return {
        "ok": True,
        "index": index,
        "indices": indices,
        "fallback_share": fallback_share(units, indices, seg.source_text),
        "damaged_count": after["damaged_count"] if after["ok"] else None,
        "damaged_indices": after["damaged_indices"] if after["ok"] else [],
        "rows": after["rows"] if after["ok"] else [],
    }
