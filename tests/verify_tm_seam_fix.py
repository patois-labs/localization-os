"""Phase 0 SEAM FIX verification (offline, hermetic).

THE DEFECT (filed): the two remaining direct TM-100% APPLY sites wrote a matched
target straight into `Segment.target_text` + `SegStatus.MT`, bypassing the sole
sanctioned write seam (`quality.commit_mt_output`) and therefore EVERY guard on it —
the refusal perimeter, the length/number/tag/meta gates and NEEDS_REVIEW routing:

  - translate._pretranslate_stream_impl  (the segmented 100% short-circuit)
  - content.apply_tm_to_document         (the manual "apply TM" action)

A POISONED TM entry (a stored hedge, a length-runaway target — exactly the poison
`tm_module.scrub_hedges` exists to clean up, and exactly what a 100%-match reintroduces
on every later document) therefore sailed straight into a segment as clean MT.

THE FIX routes both apply sites through `quality.commit_mt_output` (source="tm",
tm_eligible=False), so a TM hit clears the same gates as fresh MT. This suite proves:
  (1) a REFUSAL-shaped TM hit is now WITHHELD + routed to NEEDS_REVIEW on BOTH paths
      (pre-fix it committed the hedge as SegStatus.MT — the fail-pre-fix case);
  (2) a LENGTH-RUNAWAY TM hit is committed but routed to NEEDS_REVIEW (the soft gate);
  (3) a CLEAN TM hit still applies as SegStatus.MT on both paths (no regression).

FAIL-PRE-FIX PROOF (recorded in the builder report, per playbook §4 — NEVER git stash):
the pre-fix `translate.py`/`content.py` are recoverable via
`git show HEAD:app/services/translate.py`; temporarily restoring the direct-write block
makes tests (1)/(2) FAIL (the hedge/runaway commits as MT).

HERMETICITY: throwaway LOC_TMS_DATA_DIR via tests._bootstrap; ASCII-only; MockProvider
offline (no keys). The poisoned rows are inserted DIRECTLY (tm.write_back would itself
refuse a hedge) — modelling an entry stored BEFORE the write gate existed.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tmseam_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    Segment,
    SegmentTM,
    SegStatus,
    TargetLocale,
    TransMode,
)
from app.services import content, tm, translate

init_db()

# A hedge refusal.is_refusal catches unambiguously (a leading "I'm ready to translate"
# assistant preamble); it is the canonical poison the refusal perimeter exists for.
HEDGE = (
    "I'm ready to translate from English to French. However, I don't see any "
    "source text provided. Please provide the text you would like translated."
)
# A length-runaway: a 2-char source that came back as a 200+ char conversational
# preamble (surplus >> 120 AND ratio >> 5.0 -> trips quality.length_runaway).
RUNAWAY_SRC = "OK"
RUNAWAY_TGT = ("Bien sur, je serais ravi de vous aider avec cette traduction. " * 6).strip()


def _mk_project(s, name="SeamProj", src="en-US"):
    p = Project(name=name, source_locale=src)
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def _mk_doc(s, project_id, locale, sources):
    doc = Document(
        project_id=project_id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
    )
    s.add(doc)
    s.commit()
    s.refresh(doc)
    for i, src in enumerate(sources):
        s.add(
            Segment(
                document_id=doc.id,
                target_locale=locale,
                order_index=i,
                source_text=src,
                target_text="",
                status=SegStatus.NEW,
            )
        )
    s.add(TargetLocale(project_id=project_id, locale=locale))
    s.commit()
    return doc


def _poison(s, project_id, source, target, src="en-US", tgt="fr-FR"):
    """Insert a poisoned SegmentTM row DIRECTLY (bypassing tm.write_back's own refusal
    gate) with the correct source_hash so tm.lookup_batch returns it as a 100% hit."""
    row = SegmentTM(
        project_id=project_id,
        source_locale=src,
        target_locale=tgt,
        source_text=source,
        target_text=target,
        source_hash=tm.hash_text(source, src, tgt),
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def _seg(s, doc_id, order=0):
    from sqlmodel import select

    return s.exec(
        select(Segment).where(Segment.document_id == doc_id, Segment.order_index == order)
    ).one()


# --- (1) refusal-shaped TM hit is withheld + routed to review ----------------
def test_pretranslate_hedge_withheld():
    with Session(engine) as s:
        p = _mk_project(s)
        _poison(s, p.id, "Adventure awaits.", HEDGE)
        doc = _mk_doc(s, p.id, "fr-FR", ["Adventure awaits."])
        list(translate.pretranslate_stream(s, doc.id, "fr-FR"))
        s.expire_all()
        seg = _seg(s, doc.id)
        assert seg.status == SegStatus.NEEDS_REVIEW, (
            "pre-fix the hedge committed as MT; the seam must withhold + route it",
            seg.status,
        )
        assert seg.target_text != HEDGE, (
            "a refusal hedge must NOT be written to target_text",
            seg.target_text[:40],
        )
        assert seg.qa_flags, "the seam must attach a refusal qa_flag"
    print("  pretranslate: refusal-shaped TM hit withheld + NEEDS_REVIEW: OK")


def test_apply_tm_to_document_hedge_withheld():
    with Session(engine) as s:
        p = _mk_project(s, name="SeamApply")
        _poison(s, p.id, "Click here.", HEDGE)
        doc = _mk_doc(s, p.id, "fr-FR", ["Click here."])
        n = content.apply_tm_to_document(s, doc, p, "fr-FR")
        assert n == 0, ("a withheld hedge is not a successful apply", n)
        s.expire_all()
        seg = _seg(s, doc.id)
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        assert seg.target_text != HEDGE, seg.target_text[:40]
        assert seg.qa_flags, "the seam must attach a refusal qa_flag"
    print("  apply_tm_to_document: refusal-shaped TM hit withheld + NEEDS_REVIEW: OK")


# --- (2) length-runaway TM hit committed but routed to review ----------------
def test_pretranslate_length_runaway_routed():
    with Session(engine) as s:
        p = _mk_project(s, name="SeamRunaway")
        _poison(s, p.id, RUNAWAY_SRC, RUNAWAY_TGT)
        doc = _mk_doc(s, p.id, "fr-FR", [RUNAWAY_SRC])
        list(translate.pretranslate_stream(s, doc.id, "fr-FR"))
        s.expire_all()
        seg = _seg(s, doc.id)
        # SOFT gate: the text IS committed (length is not withheld) but the segment is
        # routed to a human -- pre-fix it was clean MT.
        assert seg.status == SegStatus.NEEDS_REVIEW, (
            "a length-runaway TM hit must route to review",
            seg.status,
        )
        assert seg.qa_flags, "the length gate must flag the segment"
    print("  pretranslate: length-runaway TM hit routed to NEEDS_REVIEW: OK")


# --- (3) a clean TM hit still applies as MT (no regression) ------------------
def test_clean_tm_hit_still_applies_pretranslate():
    with Session(engine) as s:
        p = _mk_project(s, name="SeamClean")
        _poison(s, p.id, "Adventure awaits.", "L'aventure vous attend.")
        doc = _mk_doc(s, p.id, "fr-FR", ["Adventure awaits."])
        list(translate.pretranslate_stream(s, doc.id, "fr-FR"))
        s.expire_all()
        seg = _seg(s, doc.id)
        assert seg.status == SegStatus.MT, ("a clean TM hit still applies as MT", seg.status)
        assert seg.target_text == "L'aventure vous attend.", seg.target_text
        assert seg.tm_match_pct == 100, seg.tm_match_pct
    print("  pretranslate: clean TM hit still applies as MT (no regression): OK")


def test_clean_tm_hit_still_applies_apply_tm():
    with Session(engine) as s:
        p = _mk_project(s, name="SeamClean2")
        _poison(s, p.id, "Click here.", "Cliquez ici.")
        doc = _mk_doc(s, p.id, "fr-FR", ["Click here.", "Click here."])
        n = content.apply_tm_to_document(s, doc, p, "fr-FR")
        assert n == 2, ("both empty segments filled from a clean TM hit", n)
        s.expire_all()
        seg = _seg(s, doc.id)
        assert seg.status == SegStatus.MT and seg.target_text == "Cliquez ici.", (
            seg.status,
            seg.target_text,
        )
        assert seg.tm_match_pct == 100, seg.tm_match_pct
    print("  apply_tm_to_document: clean TM hit still applies as MT (no regression): OK")


# --- B1: the FULL-TEXT TM short-circuit is the THIRD sibling site --------------
def _mk_fulltext_doc(s, project_id, locale, body):

    doc = Document(
        project_id=project_id, filename="d.txt", fmt="txt", mode=TransMode.FULL_TEXT, status="new"
    )
    s.add(doc)
    s.commit()
    s.refresh(doc)
    s.add(
        Segment(
            document_id=doc.id,
            target_locale=locale,
            order_index=0,
            source_text=body,
            target_text="",
            status=SegStatus.NEW,
        )
    )
    s.add(TargetLocale(project_id=project_id, locale=locale))
    s.commit()
    return doc


def _ft_poison(s, project_id, source, target, src="en-US", tgt="fr-FR"):
    from app.models import FullTextTM

    s.add(
        FullTextTM(
            project_id=project_id,
            source_locale=src,
            target_locale=tgt,
            source_text=source,
            target_text=target,
            source_hash=tm.hash_text(source, src, tgt),
        )
    )
    s.commit()


def test_fulltext_hedge_withheld():
    with Session(engine) as s:
        p = _mk_project(s, name="FTHedge")
        body = "The whole document body."
        _ft_poison(s, p.id, body, HEDGE)
        doc = _mk_fulltext_doc(s, p.id, "fr-FR", body)
        list(translate.pretranslate_stream(s, doc.id, "fr-FR"))
        s.expire_all()
        seg = _seg(s, doc.id)
        assert seg.status == SegStatus.NEEDS_REVIEW, (
            "a full-text TM hedge must be withheld + routed to review",
            seg.status,
        )
        assert seg.target_text != HEDGE, seg.target_text[:40]
    print("  pretranslate FULL-TEXT: TM hedge withheld + NEEDS_REVIEW: OK")


def test_fulltext_hard_lock_not_overwritten():
    from app.services import tm_lock

    with Session(engine) as s:
        p = _mk_project(s, name="FTLock")
        body = "Locked full body."
        _ft_poison(s, p.id, body, "Corps traduit.")  # a clean cached target
        doc = _mk_fulltext_doc(s, p.id, "fr-FR", body)
        seg = _seg(s, doc.id)
        seg.target_text = "Human-approved body."
        tm_lock._stamp_lock(seg, band="100", hard=True, tm_id=1, entry_id=1)
        s.add(seg)
        s.commit()
        list(translate.pretranslate_stream(s, doc.id, "fr-FR"))
        s.expire_all()
        seg = _seg(s, doc.id)
        assert seg.target_text == "Human-approved body.", (
            "the full-text short-circuit must NOT overwrite a hard lock",
            seg.target_text,
        )
    print("  pretranslate FULL-TEXT: hard lock not overwritten by a cached TM hit: OK")


if __name__ == "__main__":
    test_pretranslate_hedge_withheld()
    test_apply_tm_to_document_hedge_withheld()
    test_pretranslate_length_runaway_routed()
    test_clean_tm_hit_still_applies_pretranslate()
    test_clean_tm_hit_still_applies_apply_tm()
    test_fulltext_hedge_withheld()
    test_fulltext_hard_lock_not_overwritten()
    print("verify_tm_seam_fix: all OK")
