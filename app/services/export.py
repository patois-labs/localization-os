"""Export round-trip — rebuild the source file with translations injected.

Reconstructs the paragraph→translation map from the stored skeleton + segments,
then re-opens the ORIGINAL file and replaces only the translated paragraphs (so
images, tables, headers, and untouched formatting survive). Untranslated
paragraphs fall back to their source text.

Owns file reconstruction, paragraph text mapping, inline tag damage analysis,
and the source-fallback ceiling. Delivery gating and quarantine exceptions live
in export_gate; this module retains transitional re-exports for existing callers.

Documented in: docs/architecture/translation-pipeline.md
"""

import logging

from sqlmodel import Session, select

from app.filters import get_filter
from app.models import Document, Segment, SegStatus, TransMode

# Transitional re-export shim for one release; new code imports from export_gate.
# Names: SUB_AUTOMATIC, SUB_EXPLICIT, ExportEmptyTargetsHeld, ExportHeldLocale,
# ExportMissingBlob, ExportUnreadableBlob, TagExportQuarantine,
# _gate_held_cause_clause, _gate_held_reason_for_docs, _sub, assert_deliverable,
# gate_held_reason, gate_held_reason_for_project, held_locale_reason,
# substitution_detail.
# Private exports preserve compatibility: _gate_held_cause_clause formats hold
# causes; _gate_held_reason_for_docs aggregates document holds; _sub constructs
# substitution records used here. _source_fallback_ceiling remains defined here
# and is exported for fulltext_paragraphs, which imports it at two recovery sites.
from app.services.export_gate import (
    SUB_AUTOMATIC,
    SUB_EXPLICIT,
    ExportEmptyTargetsHeld,
    ExportHeldLocale,
    ExportMissingBlob,
    ExportUnreadableBlob,
    TagExportQuarantine,
    _gate_held_cause_clause,
    _gate_held_reason_for_docs,
    _sub,
    assert_deliverable,
    gate_held_reason,
    gate_held_reason_for_project,
    gate_held_segments,
    held_locale_reason,
    substitution_detail,
)
from app.services.maintenance import admitted_work
from app.services.segment import find_placeholders, join_unit, unit_layouts

__all__ = [
    "SUB_AUTOMATIC",
    "SUB_EXPLICIT",
    "ExportEmptyTargetsHeld",
    "ExportFileTargetMismatch",
    "ExportHeldLocale",
    "ExportMissingBlob",
    "ExportUnreadableBlob",
    "TagExportQuarantine",
    "_gate_held_cause_clause",
    "_gate_held_reason_for_docs",
    "_source_fallback_ceiling",
    "_sub",
    "assert_deliverable",
    "build_translated_file",
    "file_target_refusal",
    "gate_held_reason",
    "gate_held_reason_for_project",
    "gate_held_segments",
    "held_locale_reason",
    "paragraph_texts_for",
    "segment_tag_damage",
    "substitution_detail",
]

_LOG = logging.getLogger(__name__)


def _source_fallback_ceiling() -> float:
    """`export_source_fallback_max_share` as a float, tolerant of a junk settings row.

    Above this share of the body an explicit source-fallback override is REFUSED, not
    warned: on a 2-paragraph doc whose tagged paragraph is 90% of the text, "export
    anyway" ships an essentially untranslated file that LOOKS done — precisely the
    failure a source-fallback with no ceiling produces."""
    from app.services import settings

    try:
        val = float((settings.get("export_source_fallback_max_share") or "0.35").strip())
    except (TypeError, ValueError):
        return 0.35
    return val if 0.0 <= val <= 1.0 else 0.35


# RUNG-2 ADMISSIBILITY (adversarial review of the BUG-04 fix). A `tag_integrity` flag
# survives on a segment whose text has since been REWRITTEN CLEAN, because
# `quality.commit_mt_output` merges the flag on damage but NEVER clears it on a clean
# commit — so a re-translation that lands perfect tokens and then trips ANY other soft
# gate (a QE fail, DNT, glossary, number, nontranslation, an LQA fix-revert) re-asserts
# NEEDS_REVIEW while the OLD tag flag is still sitting in `qa_flags`. Reproduced on both
# shapes: a tagless .md segment and a token-bearing .docx segment, each exporting ENGLISH
# SOURCE over a correct, structurally clean translation — the exact FALSE-SUBSTITUTION
# direction this guard exists to avoid, and a REGRESSION against pre-BUG-04 behaviour
# (rung 1 alone shipped both).
#
# The discriminator is what the flag CLAIMS. Rung 1 has just re-derived the whole
# structural verdict from the text; a flag that names a STRUCTURAL break (dropped/added/
# duplicated/reordered token, malformed nesting, emptied span, residual sentinel) on text
# that is now clean is therefore describing a PREVIOUS VERSION of the segment — stale by
# construction, and honouring it can only substitute good text. The one verdict rung 1
# genuinely cannot re-derive is UNCONFIRMED PLACEMENT: every producer of a
# structurally-clean-but-still-flagged target names it in the reason —
# `"point-token order restored (best-guess placement — verify)"` (quality.py, the
# deterministic rung), `"AI tag repair applied (placement not verified)"` and
# `"AI tag repair applied, placement unconfirmed (…)"` (tag_llm_repair). That is the only
# family rung 2 may act on.
_PLACEMENT_MARKERS = ("placement",)


def _placement_unconfirmed(reason: str) -> bool:
    """True when a `tag_integrity` reason is a PLACEMENT claim (rung-2 admissible)."""
    low = (reason or "").lower()
    return any(m in low for m in _PLACEMENT_MARKERS)


def segment_tag_damage(seg) -> str:
    """The export's tag verdict for ONE SEGMENTED unit — "" when the unit may ship.

    TWO rungs, because each is blind to what the other sees.

    RUNG 1 — RECOMPUTE (`quality.tag_damage(source, target)`), the authoritative
    structural verdict, now evaluated for EVERY segment. It used to run only on a
    document whose SOURCE carried at least one `⟦⟧` token (a DOC-level `doc_has_tokens`
    precondition), which made the whole guard fail OPEN on exactly the case QA hit in
    production: a plain MARKDOWN source with no tags anywhere, whose model INVENTED mask
    notation — `Cliquez sur ⟦g1⟧Enregistrer⟦/g1⟧` and `⟦r1⟧%s de ⟦/r1⟧%d` were delivered
    verbatim in .md files. Only docx/html/xliff sources ever carry `⟦⟧` tokens, so for
    md/txt/po/srt/json/xlsx the precondition was ALWAYS false and `export_tag_damage_policy`
    was never consulted at all. The write seam had done its job (both segments carried a
    `tag_integrity` MAJOR flag and `needs_review`); the exporter simply never asked.
    Dropping the precondition costs nothing: `tag_damage` guarantees "" for a genuinely
    tagless PAIR — neither a well-formed token nor a stray marker character on either
    side — so a real tagless document stays byte-identical. (GitHub #43 widened that
    predicate: it now also reports MALFORMED marker debris the target carries and its
    source does not, which is the same asymmetric class by a cruder measure. A document
    that acquires a verdict under the new rung was never genuinely tagless — its target
    holds invented notation.) The precondition suppressed only the ASYMMETRIC case —
    notation in the target that the source never had — which is exactly the one that
    leaks internal markup to a customer.

    RUNG 2 — the DURABLE `tag_integrity` flag on a segment still in NEEDS_REVIEW.
    `quality.commit_mt_output` can commit text that is structurally CLEAN and STILL
    flagged: a deterministic point-order restore ("best-guess placement — verify") and an
    unverified LLM repair both write back a target whose token multiset, order and
    nesting are perfect while the PLACEMENT is unconfirmed. Rung 1 re-derives its verdict
    from the text and therefore cannot see that; the flag is the only record of it, and
    shipping an unconfirmed placement is the same class of silent formatting damage.
    Scoped to NEEDS_REVIEW on purpose: a human who reviewed the segment and moved it on
    is trusted, and the editor save re-derives or CLEARS the flag from the text actually
    written (`web/documents.py`).

    RUNG 2 IS DELIBERATELY NARROW, and the editor save is NOT the only writer — see
    `_PLACEMENT_MARKERS` above. Two independent admissibility conditions, because a stale
    flag substituting a GOOD translation with English is the one failure this guard must
    never cause:
      (a) the flag must CLAIM UNCONFIRMED PLACEMENT. Any other tag_integrity reason is a
          structural claim rung 1 has just re-derived as FALSE, i.e. stale.
      (b) the SOURCE must actually carry inline tokens. A segment with no tags has no
          placement to be unconfirmed about, so no placement flag on it can be live. This
          is a structural floor that holds even if a future reason string slips past (a).
    Rung 2 loses nothing by either: a segment whose tags are REALLY broken is caught by
    rung 1 from the text itself."""
    from app.services.quality import TAG_FLAG_TYPE, placeholder_violation, tag_damage

    reason = tag_damage(getattr(seg, "source_text", None), getattr(seg, "target_text", None))
    if reason:
        return reason
    if getattr(seg, "status", None) != SegStatus.NEEDS_REVIEW:
        return ""
    # RUNG 3 (#711) - the REAL-PLACEHOLDER verdict, recomputed from the text, on a segment
    # still HELD for review. The placeholder gate (`placeholder_violation`: a dropped or
    # added `%s` / `{name}` / `%(n)s` / `{{x}}`, a lost symbol reference, ICU structure)
    # routes such a target to NEEDS_REVIEW and keeps it out of TM, but nothing stopped the
    # export: the held target was written into the customer's file, where printf /
    # `string.Format` / the ICU compiler fails at run time. Same shape as rung 2: scoped to
    # NEEDS_REVIEW (a reviewer who approved with `acknowledge_placeholder_loss`, or edited
    # the text, is trusted) and RECOMPUTED, so a stale flag on text since fixed never
    # substitutes a good translation.
    # #716 gate: a fixed `1` for `#` in an ICU plural `one` branch is a wording finding (the
    # message compiles; most counts read right), so it holds the segment for review but never
    # turns the delivered message into English.
    ph = placeholder_violation(
        getattr(seg, "source_text", None),
        getattr(seg, "target_text", None),
        segment=seg,
        count_wording=False,
    )
    if ph:
        return ph
    if not find_placeholders(getattr(seg, "source_text", None) or ""):
        return ""  # (b) no tags -> no live placement verdict
    for f in getattr(seg, "qa_flags", None) or []:
        if not (isinstance(f, dict) and f.get("type") == TAG_FLAG_TYPE):
            continue
        flag_reason = str(f.get("reason") or f.get("message") or "")
        if _placement_unconfirmed(flag_reason):  # (a)
            return flag_reason
    return ""


def paragraph_texts_for(
    session: Session,
    doc: Document,
    locale: str,
    report: list | None = None,
    allow_source_fallback: bool = False,
    deliver_empties: bool | None = None,
    fallback_keys: set[str] | None = None,
) -> dict:
    if fallback_keys is not None and report is None:
        report = []
    # Batch 15 T2 (LAW-TM-16): the APPROVED-empty delivery emit mode. When a covering
    # DeliveryEmptyApproval releases this (doc, locale)'s empties, the empty-target segments
    # ship as EMPTY strings -- the `s.target_text or s.source_text` source-fallback is
    # SUPPRESSED for them (the maintainer's "with empty segments"). Resolved from the covering-
    # approval state HERE, at the one seam every surface funnels through, so none of the
    # seven delivery sites needs to pass a flag (§9.4). `deliver_empties` may be passed
    # explicitly by an internal caller (build_translated_file, to compute it once + audit),
    # but defaults to self-resolution when None. It ONLY zeroes empty targets on a released
    # target; a normal (non-approved) delivery is byte-identical (LAW-TM-18).
    if deliver_empties is None:
        from app.services import tm_delivery

        deliver_empties = tm_delivery.covering_approval_releases(session, doc, locale)
    units = (doc.skeleton or {}).get("units", [])
    segs = sorted(
        session.exec(
            select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == locale)
        ).all(),
        key=lambda s: s.order_index,
    )
    # AI Tagging 1.7 (GitHub #41) — the SEGMENTED deliverable must consult the tag
    # verdict. The FULL_TEXT branch below has had a quarantine guard since 1.6, but this
    # function otherwise built `targets` from `target_text` alone, consulting NEITHER
    # `Segment.status` NOR `qa_flags` NOR `tag_damage`. So the gate would detect damage,
    # flag the segment, make it TM-ineligible and withhold it from connectors — and then
    # export wrote it into the CUSTOMER'S FILE anyway, silently collapsing the formatting
    # with nothing in the artifact to signal it. Measured live on a realistic DOCX: 6 of
    # 16 formatted paragraphs lost their formatting; on an XLIFF, a unit lost its inline
    # markup entirely. Both files reopened cleanly and leaked no tokens — a dropped tag
    # does not leak, it MERGES RUNS, which is why every earlier check passed.
    #
    # Unlike FULL_TEXT (where a positional desync scrambles the WHOLE map, so a global
    # quarantine is proportionate), SEGMENTED damage is LOCAL to one unit. Default policy
    # is therefore per-segment SOURCE FALLBACK: emit the source, whose tokens are correct
    # by construction, so the document's formatting survives and that unit is visibly
    # untranslated rather than invisibly de-formatted. `quarantine` (FULL_TEXT-consistent)
    # and `ship` (the pre-1.7 behaviour, explicit opt-out) are the other options.
    # Local imports: `quality` imports tag_repair/ai_tagging and `settings` touches the
    # DB, so importing them at module load would widen export's import surface (and
    # quality already imports segment helpers this module also uses).
    from app.services import settings

    policy = (settings.get("export_tag_damage_policy") or "source").strip().lower()
    # Keyed on the SEGMENTS, not the skeleton units: segments are what this guard
    # actually inspects, they always exist, and a document whose skeleton is thin or
    # absent (fixtures, connector-created rows) would otherwise silently skip the guard
    # entirely — a fail-OPEN default on the deliverable path.
    #
    # FULL_TEXT-ONLY as of the BUG-04 fix. The SEGMENTED branch below no longer consults
    # it: a doc-level "the source has tokens somewhere" precondition is fail-OPEN for
    # INVENTED tokens (see `segment_tag_damage`), and every markdown/text/po/srt/json
    # deliverable has a tagless source, so the segmented guard was dead code on those
    # formats. FULL_TEXT keeps it — there the alternative is a GLOBAL quarantine of a
    # doc whose paragraphs simply reflowed, which would regress main on tagless bodies.
    doc_has_tokens = any(find_placeholders(s.source_text or "") for s in segs)
    targets = []
    damaged_idx: list[int] = []
    # Positions where the policy substitution had NOTHING to substitute (see the
    # `s.source_text or ""` branch below) — consulted by the SEGMENTED join.
    empty_sub: list[bool] = []
    source_sub: list[bool] = []
    for s in segs:
        text = s.target_text or s.source_text
        substituted = not s.target_text and not deliver_empties
        # LAW-TM-16: on an approved-empty delivery, an empty target ships EMPTY (its source
        # fallback suppressed). The tag-damage branch below runs only when target_text is
        # non-empty, so it is untouched by this.
        if deliver_empties and not (s.target_text or "").strip():
            text = ""
        emptied = False
        # A genuinely tagless segment is untouched (byte-identical to pre-1.7) because
        # `tag_damage` returns "" for it — that guarantee lives in the PREDICATE, which
        # is why this call no longer needs a doc-level token precondition in front of
        # it. "Tagless" means BOTH sides carry neither a well-formed token nor a stray
        # marker character (GitHub #43's residue rung). A segment whose source carries no
        # token CAN suffer tag damage: the model invents it, well-formed (BUG-04) or
        # malformed (#43).
        # SEGMENTED ONLY. In FULL_TEXT the segment IS the whole document, so a source
        # fallback would silently export the ENTIRE document untranslated — strictly
        # worse than the 1.6 quarantine, which at least tells the operator something is
        # wrong. Caught by running this against real data: doc 44 (full_text, 1 segment,
        # damaged) "exported fine" at 6175 bytes — of pure source text. FULL_TEXT keeps
        # its existing global guard below, untouched.
        if doc.mode != TransMode.FULL_TEXT and policy != "ship" and (s.target_text or "").strip():  # noqa: SIM102
            if segment_tag_damage(s):
                damaged_idx.append(s.order_index)
                if policy != "quarantine":
                    # correct tokens -> formatting survives. `or ""` because a
                    # connector-created row CAN carry a null source_text, and a None in
                    # `targets` raises inside the `" ".join(chunk)` below — the guard
                    # now runs on formats where that row shape occurs, so a substitution
                    # must never be able to crash the export it exists to make safe.
                    #
                    # But "" is CONTENT DELETION, not a source fallback: the policy's
                    # promise is that the unit ships its SOURCE and is visibly
                    # untranslated, and an empty paragraph reported as "substituted"
                    # breaks that promise in the silent direction. The row's own source
                    # is missing, not the DOCUMENT's — the skeleton still holds the unit
                    # text — so flag the position and let the join below fall back to it.
                    text = s.source_text or ""
                    substituted = True
                    emptied = not (text or "").strip()
        targets.append(text)
        empty_sub.append(emptied)
        source_sub.append(substituted)
    if damaged_idx and policy == "quarantine":
        raise TagExportQuarantine(
            # ASCII hyphen, not U+2014: this string reaches CLI stdout via
            # `doc export` / `project export`; _write_ascii_safe stops the crash but
            # renders an escape on a cp1252 console.
            "%d segment(s) carry broken inline-formatting tokens (order_index %s) - "  # noqa: UP031
            "exporting them would silently collapse formatting in the delivered file. "
            "Whole export quarantined for review."
            % (len(damaged_idx), ", ".join(str(i) for i in damaged_idx[:20])),
            doc_id=doc.id,
            locale=locale,
            kind="segments",
        )
    if damaged_idx:
        _LOG.warning(
            "export: %d tag- or placeholder-damaged segment(s) fell back to source text "
            "(doc=%s locale=%s order_index=%s)",
            len(damaged_idx),
            doc.id,
            locale,
            damaged_idx[:20],
        )
    if report is not None:
        # SEGMENT order_index, named as such in the record itself.
        report.extend(_sub("segment", i, SUB_AUTOMATIC) for i in damaged_idx)
    mapping: dict[str, str] = {}

    if doc.mode == TransMode.FULL_TEXT:
        # LAW-TM-16 / §12: an approved-empty delivery of a fully-empty FULL_TEXT body emits
        # BLANK units, skipping the under-translation guards below (the human approved the
        # empties). A FULL_TEXT doc is one segment, so "has empties" == "the body is blank";
        # reachable only when a covering approval released it (else deliver_empties is False).
        if deliver_empties and not (targets[0] if targets else "").strip():
            return {str(u["p_index"]): "" for u in units}
        full = targets[0] if targets else ""
        parts = full.split("\n\n")
        # AI Tagging 1.6 — GLOBAL split-time guard. The TAGGED and TAGLESS arms both
        # quarantine a paragraph-count desync; they differ only in the EXTRA per-paragraph
        # inline-token check the tagged arm adds. TAGGED (has_tokens): the COUNT must align
        # AND each translated paragraph must carry the SAME inline-token multiset as its
        # source paragraph (a merged/reordered blank line scrambles colliding cross-
        # paragraph ⟦r1⟧ spans), else the WHOLE export is quarantined. TAGLESS: no
        # formatting to corrupt, but a count desync is STILL a loud quarantine (honest-
        # failure, 2026-08-13) — aligned counts (on the non-blank parts) export byte-
        # identical, a desync refuses with no positional-fallback override, because a
        # silent source-fill shipped English source under a target-locale filename. See
        # TagExportQuarantine for which guard claims which case.
        #
        # SEGMENT-keyed, not skeleton-keyed (GitHub #42). The SEGMENTED sibling above
        # keys on the segments because skeleton-keying is fail-OPEN: a document whose
        # skeleton is thin or absent (fixtures, connector-created rows, a lost skeleton)
        # would silently skip the guard on the DELIVERABLE path. The two predicates ARE
        # equivalent for a real ingested FULL_TEXT doc (its segment source IS
        # "\n\n".join(unit texts), ingest.py), asserted rather than assumed.
        has_tokens = doc_has_tokens
        if has_tokens:
            if len(parts) != len(units):
                # GUARD A. The positional map is scrambled for EVERY later paragraph,
                # so there is no trustworthy per-paragraph view: no diagnosis rows, no
                # revert, and `allow_source_fallback` deliberately does not reach here.
                raise TagExportQuarantine(
                    "FULL_TEXT paragraph count desync: translated body has %d "  # noqa: UP031
                    "paragraph(s), source has %d - the positional map would scramble "
                    "formatting across paragraphs. Whole export quarantined for review."
                    % (len(parts), len(units)),
                    doc_id=doc.id,
                    locale=locale,
                    kind="count",
                )
            # GUARD B. Scan EVERY paragraph and raise ONCE. Pre-#42 this raised on the
            # FIRST mismatch and threw the token delta away, so the operator was told
            # only "paragraph N" — no missing/extra set, no idea how many others were
            # broken.
            #
            # THE PREDICATE IS `quality.tag_damage_detail`, NOT A LOCAL COMPARISON. This
            # loop used to compare `Counter(find_placeholders(...))` per paragraph, which
            # is a STRICT SUBSET of what the commit gate checks: it is blind to
            # reordering, broken nesting, a residual `«TAG_N»` sentinel and — the case
            # that shipped — an EMPTIED PAIRED SPAN, where every token survives in order
            # and only the WORDS moved out, so the run and its rPr evaporate at build
            # time. Calling the real predicate per paragraph is what makes "a rung added
            # to tag_damage is inherited everywhere" true.
            #
            # Conservative semantics (advisory dropped): ANY divergence is export-
            # blocking. A shape-preserving reorder now quarantines too; the recovery
            # surface's revert is the way out.
            from app.services.quality import ascii_reason as _ascii_reason
            from app.services.quality import tag_damage_detail as _tag_damage_detail

            bad: list[dict] = []
            for idx, u in enumerate(units):
                delta = _tag_damage_detail(u["text"], parts[idx])
                if delta:
                    bad.append(
                        {
                            "index": idx,
                            "p_index": u.get("p_index", idx),
                            "reason": delta["reason"],
                            "missing": delta["missing"],
                            "extra": delta["extra"],
                            "source_text": u["text"],
                            "target_text": parts[idx],
                        }
                    )
            if bad and allow_source_fallback:
                # C-3: a PER-EXPORT, explicitly-requested substitution — never a
                # setting, never a default. Reachable only for guard-B damage on a doc
                # whose paragraph count aligns, so the map is exact and the substitution
                # is local. Above the ceiling it is REFUSED, because an "export anyway"
                # that ships a mostly-untranslated file that looks done is worse than the
                # quarantine.
                from app.services import fulltext_paragraphs as _ftp

                src_full = segs[0].source_text if segs else "\n\n".join(u["text"] for u in units)
                idxs = [b["index"] for b in bad]
                share = _ftp.fallback_share(units, idxs, src_full)
                ceiling = _source_fallback_ceiling()
                if share > ceiling:
                    raise TagExportQuarantine(
                        "FULL_TEXT source fallback refused: the %d damaged paragraph(s) "  # noqa: UP031
                        "cover %.0f%% of the document body, above the "
                        "export_source_fallback_max_share ceiling of %.2f - exporting "
                        "would ship a substantially UNTRANSLATED file that looks "
                        "finished. Whole export quarantined for review."
                        % (len(bad), share * 100.0, ceiling),
                        doc_id=doc.id,
                        locale=locale,
                        kind="share_ceiling",
                        paragraphs=bad,
                    )
                for b in bad:
                    parts[b["index"]] = units[b["index"]]["text"]
                if report is not None:
                    # PARAGRAPH indices, and an EXPLICIT kind.
                    report.extend(_sub("paragraph", i, SUB_EXPLICIT) for i in idxs)
                _LOG.warning(
                    "export: FULL_TEXT source fallback for %d paragraph(s) "
                    "(doc=%s locale=%s index=%s share=%.3f)",
                    len(bad),
                    doc.id,
                    locale,
                    idxs[:20],
                    share,
                )
                bad = []
            if bad:
                first = bad[0]
                msg = (
                    "FULL_TEXT paragraph %d inline-tag set desync: the translated "  # noqa: UP031
                    "paragraph's formatting tokens no longer match the source "
                    "paragraph (a cross-paragraph shift the per-segment gate cannot "
                    "see). Whole export quarantined for review." % first["p_index"]
                )
                if len(bad) > 1:
                    # Appended ONLY — the first sentence stays byte-compatible for the
                    # five `str(exc)`-only handlers.
                    msg += (
                        " %d paragraph(s) affected (index %s)."  # noqa: UP031
                        % (  # noqa: RUF100, UP031
                            len(bad),
                            ", ".join(str(b["index"]) for b in bad[:20]),
                        )
                    )
                # Also appended only. Without it an EMPTIED SPAN (every token present,
                # missing/extra both empty) reads as "the tokens no longer match" with
                # nothing naming what actually broke.
                msg += " Damage: {}.".format(_ascii_reason(first["reason"]))
                raise TagExportQuarantine(
                    msg, doc_id=doc.id, locale=locale, kind="tokens", paragraphs=bad
                )
        elif units:
            # TAGLESS FT arm (has_tokens False; #11 skips skeleton-less/fixture docs with
            # no units, preserving main's behavior there). The tagged arm above owns its
            # count + token guards; this arm used to fall back to SOURCE text POSITIONALLY
            # and SILENTLY whenever the translated body desynced from the source count.
            # Honest-failure finding (2026-08-13 big-doc flood): a sentence-tuned model
            # emitted one paragraph for a whole doc, and this arm served 851 ENGLISH
            # sentences under an "es" filename via `parts[idx] if idx < len(parts) else
            # u["text"]` below — a target-locale deliverable that was ENGLISH SOURCE, with
            # zero warning.
            #
            # A count desync means NO per-paragraph view is trustworthy — an INTERIOR drop
            # shifts every later paragraph — so, exactly like GUARD A on the tagged arm,
            # this is an UNCONDITIONAL quarantine: `allow_source_fallback` does NOT open a
            # door here (there is no trustworthy positional map to substitute against).
            # Re-run or hand-edit the body is the only way out.
            #
            # The comparison + mapping run through the SHARED `align_parts` predicate, so
            # split_view / revert / set-paragraph and this deliverable agree on STRUCTURE
            # (finding 4) — they legitimately DIFFER only on delivery-readiness (split_view
            # shows a cleared paragraph as a blank row, export refuses to ship it). It is
            # SYMMETRIC — non-blank PARTS vs non-blank UNITS — which is DEFENSE IN DEPTH,
            # not a bare filter-list claim: the FT filters skip whitespace-only units as of
            # 2026-08-13 (docx/pdf/text/markdown/html always did; xliff now guards
            # `<source>.strip()`), but PO keeps a whitespace `msgid` for round-trip safety,
            # and legacy rows ingested before this change may still carry a blank unit — the
            # symmetric predicate maps those blank units to their own (blank) source and
            # matches the real paragraph counts, so a genuine XLIFF/PO doc with a blank
            # source unit still exports (translation OR source-only) instead of being
            # permanently quarantined. Blank "\n\n" split artifacts (doubled delimiter,
            # leading/trailing blank line) are absorbed; a blank TRANSLATION of a real
            # paragraph (an NBSP-only output) drops a real part and refuses — a blank
            # paragraph is not a translation, the body editor is the way out.
            if not full.strip() and any((u.get("text") or "").strip() for u in units):
                # #10: a whitespace-only STORED target body (target_text = "   ") is NOT
                # the sanctioned held export. An untranslated locale stores target_text ""
                # (the NOT-NULL default), so `text = s.target_text or s.source_text` above
                # makes `full` the SOURCE body — non-blank and count-aligned — and this
                # branch is never reached for it (verify_export_held_locale untouched). A
                # blank STORED body would otherwise ship all-English silently (even under
                # allow_held); refuse loud.
                raise TagExportQuarantine(
                    # ASCII hyphens (this reaches CLI stdout on cp1252 consoles).
                    "FULL_TEXT under-translation: the stored translated body is blank "
                    "while the source has real paragraph(s) - exporting would ship the "
                    "ENGLISH source as the target. Re-run the translation.",
                    doc_id=doc.id,
                    locale=locale,
                    kind="untranslated",
                )
            from app.services import fulltext_paragraphs as _ftp

            aligned, _desync = _ftp.align_parts(full, units)
            if _desync == "under":
                # Fewer real translated paragraphs than real source paragraphs. TWO shapes,
                # distinguished by whether the raw split already had one part per unit — the
                # message must be TRUE for each (review MED-1):
                real_units = sum(1 for u in units if (u.get("text") or "").strip())
                if len(parts) == len(units):
                    # Counts COINCIDE: specific real-source position(s) are BLANK in the
                    # translated body (a cleared / blank paragraph). English source does NOT
                    # ship in this shape — the blank ships blank — so NAME the paragraphs
                    # and the fill/revert path, don't claim source substitution.
                    blank_idx = [
                        i
                        for i, (p, u) in enumerate(zip(parts, units, strict=False))
                        if not p.strip() and (u.get("text") or "").strip()
                    ]
                    raise TagExportQuarantine(
                        "FULL_TEXT under-translation: paragraph(s) %s of %d are blank in "  # noqa: UP031
                        "the translated body - a blank paragraph is not a translation. "
                        "Fill them (set-paragraph) or revert them to source "
                        "(revert-paragraph), then re-export."
                        % (", ".join(str(i) for i in blank_idx), len(units)),
                        doc_id=doc.id,
                        locale=locale,
                        kind="untranslated",
                    )
                # Counts DIFFER: the translated paragraphs ran OUT, so the positional map
                # would fall the tail units back to ENGLISH SOURCE (the original bug shape).
                n_real_parts = len([p for p in parts if p.strip()])
                raise TagExportQuarantine(
                    "FULL_TEXT under-translation: the translated body has %d real "  # noqa: UP031
                    "paragraph(s), the source has %d - exporting would silently "
                    "substitute ENGLISH source text for the %d missing paragraph(s). "
                    "Re-run the translation or edit the full-text body."
                    % (n_real_parts, real_units, real_units - n_real_parts),
                    doc_id=doc.id,
                    locale=locale,
                    kind="untranslated",
                )
            if _desync == "over":
                # EXTRA translated paragraphs make the positional map untrustworthy and
                # would silently DROP translated content. kind="count" reuses guard A's
                # no-way-out contract (no per-paragraph view, no override).
                real_units = sum(1 for u in units if (u.get("text") or "").strip())
                n_real_parts = len([p for p in parts if p.strip()])
                raise TagExportQuarantine(
                    "FULL_TEXT paragraph count desync: the translated body has %d real "  # noqa: UP031
                    "paragraph(s), the source has %d - the extra paragraph(s) make the "
                    "positional map untrustworthy and translated content would be "
                    "silently dropped. Re-run the translation. Whole export "
                    "quarantined for review." % (n_real_parts, real_units),
                    doc_id=doc.id,
                    locale=locale,
                    kind="count",
                )
            # Aligned: `aligned[idx]` is unit idx's target (a whitespace-only unit maps to
            # its own blank source). Feed it to the shared mapping loop below.
            parts = aligned
        # THE DURABLE FALLBACK IS A SUBSTITUTION TOO. Everything above reports the
        # substitutions THIS CALL performed. A paragraph reverted by
        # `fulltext_paragraphs.revert_paragraph` was substituted in an EARLIER call: its
        # English source text is already sitting in `seg.target_text`, so by the time the
        # export runs the body is structurally perfect and nothing here had any reason to
        # notice. The provenance is durable, so the report must be too — read HERE, at
        # the one seam every surface funnels through, so no surface can be silent by
        # omission while the sanctioned `allow_source_fallback` door refuses the same
        # file at the ceiling.
        if report is not None and segs:
            from app.services.quality import source_fallback_indices as _sfi

            _already = {
                e.get("index")
                for e in report
                if isinstance(e, dict) and e.get("unit") == "paragraph"
            }
            report.extend(
                _sub("paragraph", i, SUB_EXPLICIT)
                for i in _sfi(segs[0].qa_flags)
                if i not in _already
            )
        substituted_paragraphs = {
            e.get("index") for e in (report or []) if e.get("unit") == "paragraph"
        }
        for idx, u in enumerate(units):
            mapping[str(u["p_index"])] = parts[idx] if idx < len(parts) else u["text"]
            if fallback_keys is not None and (
                any(source_sub) or idx >= len(parts) or idx in substituted_paragraphs
            ):
                fallback_keys.add(str(u["p_index"]))
    else:
        # Use the SAME segmentation IDENTITY (locale + engine/ruleset) that ran at
        # ingest (stamped in the skeleton) so the sentence count here matches
        # exactly — otherwise a toggled srx_enabled or a swapped .srx would
        # re-split a paragraph differently and misalign the stored target segments
        # against the paragraphs (finding F1). segments_per_unit encapsulates that
        # stamped re-split (shared with image_localization).
        # #668/#671: the same stamped split also yields each unit's whitespace layout
        # (edge whitespace + the separator between its sentences), so the rejoin below
        # restores what the trimmed segments lost instead of a blanket `" ".join`.
        layouts = unit_layouts(doc)
        # #675: the count must be EXACTLY what ingest wrote. A whitespace-only unit (an XSLT
        # `&#160;`) got ZERO segments at ingest, so a `max(1, ...)` here took the next
        # unit's segment for it and shifted every later unit by one. Such a unit has
        # nothing to translate: it is written back as its own source text.
        counts = [len(lay["pieces"]) for lay in layouts]
        i = 0
        for u, n, lay in zip(units, counts, layouts, strict=False):
            if n == 0:
                mapping[str(u["p_index"])] = u["text"]
                continue
            chunk = targets[i : i + n]
            blanks = empty_sub[i : i + n]
            if fallback_keys is not None and (not chunk or any(source_sub[i : i + n])):
                fallback_keys.add(str(u["p_index"]))
            i += n
            if deliver_empties:
                # LAW-TM-16 (A-H1): an approved-empty delivery ships the EMPTY targets as
                # EMPTY, not as a lone-space artifact. DROP the empty members before joining
                # so a partially-empty unit is "filled1 filled2" (no doubled/edge spaces) and
                # an ALL-empty unit is exactly "" (not " " from `" ".join(["",""])`, which a
                # PO writer emits as a whitespace msgstr that renders blank instead of the
                # msgid). deliver_empties suppresses the SOURCE fallback for approved empties.
                keep = [k for k, t in enumerate(chunk) if (t or "").strip()]
                joined = join_unit([chunk[k] for k in keep], lay, positions=keep)
                # A2-L1: it does NOT suppress the STRUCTURAL skeleton fallback -- a
                # tag-damage-emptied NULL-source member (`blanks`) or a zero-segment unit must
                # still emit the unit's own skeleton text, never a silently-lost paragraph.
                # L2 leg-4 trade-off: a unit MIXING an approved-empty segment with a
                # tag-damage-emptied NULL-source segment ships the FULL source paragraph here
                # (never-lose-a-paragraph wins over LAW-TM-16's empty-emit) -- a rare corner
                # where the tag-damage skeleton fallback takes precedence over deliver-empties.
                if not joined.strip() and (not chunk or any(blanks)):
                    joined = u["text"]
                mapping[str(u["p_index"])] = joined
                continue
            joined = join_unit(chunk, lay) if chunk else u["text"]
            # A substitution that had no segment-level source to fall back to (a row with
            # a NULL source_text) would emit an EMPTY unit — the deliverable would silently
            # LOSE that paragraph while the report calls it "untranslated". The skeleton is
            # the document's own source of record for the unit, so use it. Narrow by
            # construction: reached only when a substitution blanked a position AND the
            # whole unit came out empty, so a legitimately empty unit (whose skeleton text
            # is empty too) and every clean document are byte-identical.
            if any(blanks) and not joined.strip():
                joined = u["text"]
            mapping[str(u["p_index"])] = joined
    return mapping


# #734 gate: the HTML walk now leaves `translate="no"` / `class="notranslate"` content out of
# the unit list, so a document of a format that runs that walk, imported BEFORE #734 and
# carrying the marker, has stored unit indexes that no longer address the same text. fmt html
# already refuses such a document (the guard above); these formats had no guard and would
# write translations into the wrong units. Only a document that can carry the marker is
# re-parsed (cheap byte test for text formats, member scan for zip formats).
_HTML_WALK_FMTS = frozenset(("phtml", "php", "jsp", "aspx", "erb", "shtml", "epub", "flprjzip"))
_ZIP_WALK_FMTS = frozenset(("epub", "flprjzip"))


def _may_carry_translate_attr(fmt: str, original: bytes) -> bool:
    if fmt not in _ZIP_WALK_FMTS:
        return b"translate" in bytes(original).lower()
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(original)) as zf:
            for info in zf.infolist():
                name = info.filename.lower()
                if name.endswith((".htm", ".html", ".xhtml")) and b"translate" in (
                    zf.read(info).lower()
                ):
                    return True
    except Exception:
        return False  # the build itself reports an unreadable archive
    return False


def _refuse_moved_html_walk(filt, original: bytes, doc) -> None:
    from app.filters.a11y import is_a11y_style

    stored = [
        (u["p_index"], u["text"])
        for u in (doc.skeleton or {}).get("units", [])
        if not is_a11y_style(u.get("style"))
    ]
    if not stored:
        return
    try:
        fresh_units = filt.parse(original).skeleton["units"]
    except Exception:
        return  # parse failures surface from the build with their own message
    fresh = [(u["p_index"], u["text"]) for u in fresh_units if not is_a11y_style(u.get("style"))]
    if stored != fresh[: len(stored)]:
        raise ExportUnreadableBlob(
            f"{doc.fmt.upper()} extraction has changed. Re-import the original file and review "
            "its translations before exporting.",
            doc_id=doc.id,
            fmt=doc.fmt,
        )


def _nothing_written(session: Session, doc: Document, locale: str, filt) -> bool:
    """B-bytes: True when the export would write no translated change into the file.

    No segment of this target holds a translation (an untouched file: the export would
    only put the source back), or, for a monolingual format with no language declaration
    (its build_localized is plain build), every translation equals its source. A format
    that declares the target language (HTML lang, XLIFF target-language, ...) or writes
    targets beside the source (PO, XLIFF, ...) is a real change once anything is written,
    so only the first case applies to it."""
    from app.filters.base import Filter

    if session is None or getattr(filt, "passthrough_untouched", True) is not True:
        return False  # no session (a unit-test seam) or a format whose deliverable differs
    rows = session.exec(
        select(Segment.source_text, Segment.target_text).where(
            Segment.document_id == doc.id, Segment.target_locale == locale
        )
    ).all()
    written = [(src, tgt) for src, tgt in rows if tgt]
    if not written:
        return True
    plain = type(filt).build_localized is Filter.build_localized and not filt.writes_targets
    return plain and all(tgt == src for src, tgt in written)


def export_as_formats(fmt: str) -> tuple[str, ...]:
    """The other formats a document of `fmt` can be delivered in (`as_fmt`): a compiled
    gettext MO catalog can also come back as its PO source. Empty for every other format."""
    return {"mo": ("po",)}.get((fmt or "").lower(), ())


class ExportFileTargetMismatch(ExportUnreadableBlob):
    """#687: the document is a bilingual vendor file whose own target language is not
    this locale. Every single-document door already answers ExportUnreadableBlob with its
    message (409); a bundle skips and reports the pair instead."""


def file_target_refusal(doc, locale: str, original: bytes | None = None) -> str | None:
    """#687: the refusal message when `doc` is a bilingual vendor file that declares a
    target language other than `locale`, else None. A document stamped at ingest is read
    from its skeleton; one uploaded before the rule, from `original` when given."""
    from app.services import bilingual_target as bt

    if (getattr(doc, "fmt", "") or "").lower() not in bt.BILINGUAL_FMTS:
        return None
    skel = getattr(doc, "skeleton", None) or {}
    declared = skel.get(bt.SKELETON_KEY) if isinstance(skel, dict) else None
    if not declared and original is not None:
        declared = bt.declared_targets(doc.fmt, original)
    if declared and not bt.allows({bt.SKELETON_KEY: declared}, locale):
        return bt.locale_refusal(declared, locale)
    return None


def _refuse_other_file_target(doc, original: bytes, locale: str) -> None:
    """#687: never build a bilingual vendor file for a locale it does not declare."""
    msg = file_target_refusal(doc, locale, original)
    if msg:
        raise ExportFileTargetMismatch(msg, doc_id=doc.id, fmt=doc.fmt)


def _in_document_code_page(fn):
    """Decision 22: an export reads the stored original in the code page
    its import used (the skeleton's `text_code_page` stamp: the project's source language);
    a document without the stamp is read by the #509 rule, exactly as before."""
    import functools

    @functools.wraps(fn)
    def wrapped(session, doc, *args, **kwargs):
        from app.filters.base import document_code_page

        with document_code_page(getattr(doc, "skeleton", None)):
            return fn(session, doc, *args, **kwargs)

    return wrapped


@admitted_work(operation="export")
@_in_document_code_page
def build_translated_file(
    session: Session,
    doc: Document,
    locale: str,
    report: list | None = None,
    allow_source_fallback: bool = False,
    _marker_cache: dict | None = None,
    as_fmt: str | None = None,
) -> bytes:
    """The translated deliverable's bytes.

    `as_fmt` names another format to deliver in, only where `export_as_formats(doc.fmt)`
    allows it (a gettext MO upload exported as PO); None is the upload's own format.

    `report` is the #41 SUBSTITUTION channel and it is the ONLY way a caller can tell a
    clean deliverable from a degraded one: when the tag guard falls a damaged segment
    back to its source text the bytes still look perfectly well-formed, so the fact has
    to ride ALONGSIDE them (header / JSON field / manifest). It is a list of
    `{unit, index, kind}` dicts extended in place by `paragraph_texts_for`, and
    `substitution_detail()` renders it for audit rows / CLI / manifests. `unit` is
    `"segment"` (an `order_index`, the SEGMENTED path) or `"paragraph"` (a paragraph
    index within a FULL_TEXT body); `kind` is `SUB_AUTOMATIC` (#41 policy) or
    `SUB_EXPLICIT` (an operator-requested `allow_source_fallback` / a durable revert).
    Passing None keeps the pre-1.7 behaviour byte-for-byte.

    `allow_source_fallback` (GitHub #42, C-3) is the EXPLICIT per-export override for a
    FULL_TEXT guard-B desync: substitute the SOURCE text for the damaged paragraph(s)
    and report them through `report`, instead of quarantining the whole document. It is
    a per-call argument on purpose — never a setting and never a default — bounded by
    `export_source_fallback_max_share`, and it does not touch guard A."""
    from app.ports import blobstore

    # Batch 15 T2 (§9.4/§9.7): resolve the approved-empty release ONCE here (so the emit
    # mode and the `delivery.empty_delivered` audit event agree and it is computed a single
    # time), and pass it down. When no covering approval releases this target, it is False
    # and the delivery is byte-identical to today (LAW-TM-18).
    from app.services import tm_delivery

    _released = tm_delivery.covering_approval_releases(session, doc, locale, _marker_cache)
    # Compute the paragraph map FIRST so a FULL_TEXT tag-desync quarantine (1.6) is
    # raised before we even read the upload blob — the export is refused as a whole, so
    # touching storage would be wasted work (and lets callers catch TagExportQuarantine
    # without a blob dependency).
    fallback_keys: set[str] = set()
    mapping = paragraph_texts_for(
        session,
        doc,
        locale,
        report=report,
        allow_source_fallback=allow_source_fallback,
        deliver_empties=_released,
        fallback_keys=fallback_keys,
    )
    if _released:
        # The approved-empty delivery actually assembled -> audit it (§9.7). A system event
        # (the human decision was already audited at approval time). Best-effort.
        try:
            from app.services import audit as _audit

            _audit.log(
                actor="system",
                action="delivery.empty_delivered",
                target="document %s %s" % (doc.id, locale),  # noqa: UP031
                detail="approved-empty delivery assembled",
                customer_id=_audit.customer_for_document(doc.id),
            )
        except Exception:  # an audit failure must never break a delivery
            _LOG.warning(
                "audit write failed for delivery.empty_delivered (document %s %s)",
                doc.id,
                locale,
                exc_info=True,
            )
    # Issue #54 L3 (+ gate review F4): reading and decoding the original blob has THREE
    # honest failure modes, kept distinct so none is mislabeled:
    #   * ABSENT  (FileNotFoundError / NotADirectoryError) -> ExportMissingBlob, 4xx. The
    #     arm is NARROW on purpose: a PermissionError / disk IO fault is a real OSError
    #     infra failure and must keep its 5xx, not be demoted to a bogus "file missing" 409.
    #   * UNREADABLE at read time (a failed at-rest decrypt: cryptography's InvalidToken
    #     from a rotated/wrong ENCRYPTION_KEY) -> ExportUnreadableBlob, 4xx, distinct msg.
    #   * UNREADABLE at build time (the blob is fine bytes but a corrupt archive:
    #     zipfile.BadZipFile from the docx/xlsx filter) -> ExportUnreadableBlob, same class.
    from zipfile import BadZipFile

    from cryptography.fernet import InvalidToken

    _missing = (
        "the document's original uploaded file is missing from storage, so it "
        "cannot be exported (the source file is required to rebuild the "
        "translated document)"
    )
    _corrupt = (
        "the document's original uploaded file is present but unreadable / "
        "corrupt, so it cannot be exported (restore the original upload)"
    )
    try:
        original = blobstore.get().get(blobstore.document_key(doc))
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ExportMissingBlob(_missing, doc_id=doc.id, fmt=doc.fmt) from exc
    except InvalidToken as exc:
        raise ExportUnreadableBlob(_corrupt, doc_id=doc.id, fmt=doc.fmt) from exc
    filt = get_filter(doc.fmt)
    _refuse_other_file_target(doc, original, locale)
    if filt.fmt == "html":
        # Old HTML extraction split quoted attributes at a raw greater-than sign.
        # Stored paragraph indices from that scanner must never address the new units.
        # #509: a converted (Windows-1252 or declared charset) upload is re-read the way
        # it was imported; anything else the way pre-#509 imports were (never refused).
        converted = bool((doc.skeleton or {}).get("source_encoding"))
        fresh = filt.parse(original, lenient=not converted, enforce_caps=False).skeleton["units"]
        stored = (doc.skeleton or {}).get("units", [])
        old_units = [(u["p_index"], u["text"]) for u in stored]
        new_units = [(u["p_index"], u["text"]) for u in fresh]
        # Full-text uploads and older accessibility extraction can omit trailing attrs.
        if stored and old_units != new_units[: len(old_units)]:
            raise ExportUnreadableBlob(
                "HTML extraction has changed. Re-import the original HTML and review its "
                "translations before exporting.",
                doc_id=doc.id,
                fmt=doc.fmt,
            )
    elif filt.fmt in _HTML_WALK_FMTS and _may_carry_translate_attr(filt.fmt, original):
        _refuse_moved_html_walk(filt, original, doc)
    if as_fmt is None and not _released and _nothing_written(session, doc, locale, filt):
        # B-bytes (2026-10-11, decision 1): an export with no translated change comes
        # back exactly as uploaded, byte for byte.
        return bytes(original)
    from app.filters.base import InputLimitError
    from app.filters.xml_text import XmlTargetError

    try:
        # build_localized = build() + the TARGET language declaration (VPAT 504.2.2).
        if filt.fmt == "mo" and as_fmt == "po":
            from app.filters.mo_filter import build_po

            return build_po(original, mapping, locale, fallback_keys=fallback_keys)
        if filt.fmt in (
            "po",
            "mo",
            "sdlxliff",
            "mqxliff",
            "mxliff",
            "ttx",
            "txlf",
            "txml",
            "sdlrpx",
            "mqxlz",
            "xlz",
        ):
            return filt.build_localized(original, mapping, locale, fallback_keys=fallback_keys)
        if filt.fmt in ("hhc", "hhk"):
            # Decision 21: read the file in the code page the import used (the .hhp
            # project's language), never a fresh Windows-1252 guess.
            cp = (getattr(doc, "skeleton", None) or {}).get("hhp_code_page")
            return filt.build_localized(
                original, mapping, locale, hhp_code_page=cp if isinstance(cp, int) else None
            )
        if filt.fmt == "docx":
            # #349/#355: rebuild under the table walk the project was imported with. A
            # skeleton without the docx_walk stamp was numbered by the pre-#349 walk.
            from app.filters.docx_filter import walk_version

            try:
                walk = walk_version(getattr(doc, "skeleton", None))
            except ValueError as exc:
                # An unknown stamp is a refusal the user can act on (re-import), not the
                # generic rebuild failure the ValueError branch below reports (#355 gate).
                raise ExportUnreadableBlob(str(exc), doc_id=doc.id, fmt=doc.fmt) from exc
            return filt.build_localized(original, mapping, locale, walk=walk)
        if getattr(filt, "takes_columns", False) and (getattr(doc, "skeleton", None) or {}).get(
            "table"
        ):
            # Batch 6: CSV/TSV and XLSX column mode rebuild under the column plan they
            # were imported with (which column each target language writes into).
            # Units that fell back to the source (untranslated) leave their target cell
            # as it is instead of copying the source text into the target column.
            return filt.build_localized(
                original,
                mapping,
                locale,
                table=doc.skeleton["table"],
                fallback_keys=fallback_keys,
            )
        if filt.fmt == "json":
            # Batch 8: rebuild with the dialect the file was imported with (a skeleton
            # without one was imported by the generic walk), never a fresh sniff.
            dialect = (getattr(doc, "skeleton", None) or {}).get("dialect") or "plain"
            return filt.build_localized(original, mapping, locale, dialect=dialect)
        if filt.fmt in ("md", "mdx"):
            # #731: rebuild under the walk the document was imported with (no stamp = the
            # pre-#731 walk, where front matter lines were ordinary line units).
            from app.filters.markdown_filter import md_fm_keys, md_walk_version

            try:
                md_walk = md_walk_version(getattr(doc, "skeleton", None))
            except ValueError as exc:
                raise ExportUnreadableBlob(str(exc), doc_id=doc.id, fmt=doc.fmt) from exc
            return filt.build_localized(
                original,
                mapping,
                locale,
                md_walk=md_walk,
                fm_keys=md_fm_keys(getattr(doc, "skeleton", None)),
            )
        if filt.fmt == "xlsx":
            # #685: rebuild under the walk the workbook was imported with (no stamp = cells
            # only; walk 2 adds the text-box paragraphs after the cells).
            from app.filters.xlsx_filter import walk_version as xlsx_walk_version

            try:
                xlsx_walk = xlsx_walk_version(getattr(doc, "skeleton", None))
            except ValueError as exc:
                raise ExportUnreadableBlob(str(exc), doc_id=doc.id, fmt=doc.fmt) from exc
            return filt.build_localized(original, mapping, locale, walk=xlsx_walk)
        if filt.fmt == "pptx":
            # the PowerPoint walk stamp: an unknown one (a rollback) is refused, not guessed
            from app.filters.pptx_filter import walk_version as pptx_walk_version

            try:
                pptx_walk_version(getattr(doc, "skeleton", None))
            except ValueError as exc:
                raise ExportUnreadableBlob(str(exc), doc_id=doc.id, fmt=doc.fmt) from exc
        return filt.build_localized(original, mapping, locale)
    except XmlTargetError as exc:
        raise ExportUnreadableBlob(str(exc), doc_id=doc.id, fmt=doc.fmt) from exc
    except InputLimitError:
        raise  # a size/limit refusal (#224, 413) is not an unreadable blob
    except ValueError as exc:
        if filt.fmt not in (
            "docx",
            "xlsx",
            "xliff",
            "pptx",
            "sdlxliff",
            "mqxliff",
            "mxliff",
            "txlf",
            "txml",
            "mqxlz",
            "xlz",
            "ttml",
            "yaml",
            "arb",
            "mo",
            "idml",
            "sdlrpx",
            "xml",
            "dita",
            "docbook",
            "stringsdict",
            "xcstrings",
            "odt",
            "ods",
            "odp",
            "odg",
            "resx",
            "wxl",
            "ts",
            "eml",
            "ttx",
            "xmb",
            "xtb",
            "epub",
            "csv",
            "tsv",
            "scc",
            "stl",
            "desktop",
            "catkeys",
            "icml",
            "xcloc",
            "json5",
            "xslt",
            "xtg",
            "mif",
            "sub",
        ):
            raise
        _LOG.warning("export rebuild failed for document %s %s", doc.id, locale, exc_info=True)
        raise ExportUnreadableBlob(
            "The translated file could not be rebuilt. Contact support with the document ID.",
            doc_id=doc.id,
            fmt=doc.fmt,
        ) from exc
    except BadZipFile as exc:
        raise ExportUnreadableBlob(_corrupt, doc_id=doc.id, fmt=doc.fmt) from exc
