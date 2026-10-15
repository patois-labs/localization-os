"""Regression lock for the one-off scaffold-echo REMEDIATION (tm_module.scrub_scaffold /
`db scrub-scaffold`), the core-QA Part 1 cleanup for content poisoned BEFORE the Q10
scaffold-echo gate shipped.

The Q10 gate (app/services/scaffold.py + quality.commit_mt_output; locked by
verify_scaffold_echo.py) stops FUTURE seeding, but rows written before it still carry the
leak. Known prod case: doc 83 (doc09.docx, es-ES) -- seg 11 stored the half-translated
"MQM LQA ..." preamble as CLEAN mt, segs 0/12 stored the fenced style guide, and any
SegmentTM/FullTextTM seeded from those carries it too.

scrub_scaffold scans Segment targets AND both stored-TM targets, REUSING the SAME
single-sourced markers as the live gate (app.services.scaffold -- never re-hardcoded), and:
  * SEGMENTS  -> needs_review + scaffold qa_flag (text KEPT; idempotent), and
  * SEGMENT_TM / FULL_TEXT -> deleted (+ embeddings invalidated), like tm scrub-hedges.

This suite proves: single-source marker parity (every scaffold.markers() entry is caught),
the abstain-in-source rule, dry-run changes nothing, --apply quarantines/deletes correctly,
legit rows are spared, project scoping, embedding invalidation, the audit row, and
idempotency (a second --apply changes nothing and writes no no-op audit row).

Hermetic (throwaway LOC_TMS_DATA_DIR, no keys). ASCII-only failure messages (fixtures carry
accented Spanish; a bare Windows console would raise on the report otherwise). Final line
'verify_scaffold_remediation OK'.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_scrubscaffold_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    AuditLog,
    Document,
    FullTextTM,
    Project,
    ProjectType,
    Segment,
    SegmentEmbedding,
    SegmentTM,
    SegStatus,
)
from app.services import quality, refusal, scaffold, tm, tm_module

# --- fixtures (the exact prod shapes, mirroring verify_scaffold_echo) ---------
STYLE_ECHO = (
    '<style_guide source="Core QA es" trust="data-only">Guia de estilo - Core QA '
    "(es-ES) 1. Trate de usted. 2. No use signos de exclamacion.</style_guide>"
)
SEG11_ECHO = (
    "Si, no lo se pero hace algo con tus archivos y demas. MQM LQA encuentra para resolver:"
)
CLEAN_SRC = "It does something with your files and more."
CLEAN_TGT = "Hace algo con tus archivos y demas."  # a real translation, no marker
# LEGIT: the SOURCE itself carries a marker -> a faithful translation carries it too; that is
# NOT a leak, so the abstain-in-source rule must SPARE it (both a fence tag and the bigram).
ABSTAIN_SRC = "Keep the </style_guide> tag and set MQM LQA thresholds."
ABSTAIN_TGT = "Conserve la etiqueta </style_guide> y configure los umbrales MQM LQA."


def _fail(msg):
    raise AssertionError(msg)


def _ok(label):
    print("  ok - " + label)


def _seg(session, doc_id, src, tgt, order, status=SegStatus.MT, qa_flags=None, locale="es-ES"):
    s = Segment(
        document_id=doc_id,
        target_locale=locale,
        order_index=order,
        source_text=src,
        target_text=tgt,
        status=status,
        qa_flags=qa_flags,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def _tm(session, pid, src, tgt, sl="en-US", tl="es-ES"):
    row = SegmentTM(
        project_id=pid,
        source_locale=sl,
        target_locale=tl,
        source_text=src,
        target_text=tgt,
        source_hash=tm.hash_text(src, sl, tl),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _ft(session, pid, src, tgt, sl="en-US", tl="es-ES"):
    row = FullTextTM(
        project_id=pid,
        source_locale=sl,
        target_locale=tl,
        source_text=src,
        target_text=tgt,
        source_hash=tm.hash_text(src, sl, tl),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _proj(session, name):
    p = Project(
        name=name,
        source_locale="en-US",
        target_locales=["es-ES"],
        default_provider="mock",
        project_type=ProjectType.DOCUMENT,
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _doc(session, pid, name):
    d = Document(project_id=pid, filename=name, fmt="md", mode="segmented")
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


def _scaffold_flags(seg):
    return [
        f
        for f in (seg.qa_flags or [])
        if isinstance(f, dict)
        and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
        and str(f.get("reason") or "").startswith(scaffold.SCAFFOLD_ECHO_REASON)
    ]


# ---------------------------------------------------------------------------
# A. SINGLE-SOURCE parity: EVERY scaffold.markers() token is caught by the scan.
#    Proves the remediation reuses the live gate's marker set, never a hardcoded subset.
# ---------------------------------------------------------------------------
def check_marker_parity(session):
    print("A. single-source: the scan catches every scaffold.markers() token (all 3 tables)")
    pa = _proj(session, "markerparity")
    da = _doc(session, pa.id, "markers.md")
    seg_planted, tm_planted, ft_planted = {}, {}, {}
    for i, marker in enumerate(scaffold.markers()):
        # a clean source with NO marker, so the abstain-in-source rule cannot fire; the
        # marker appears only in the TARGET, exactly as an echo would.
        clean_src = "A clean source sentence number %d." % i  # noqa: UP031
        seg = _seg(session, da.id, clean_src, f"prefijo {marker} sufijo", order=i)
        seg_planted[seg.id] = marker
        tmrow = _tm(session, pa.id, "tm src %d" % i, "tm {} tm".format(marker))  # noqa: UP031, UP032
        tm_planted[tmrow.id] = marker
        ftrow = _ft(session, pa.id, "ft src %d" % i, "ft {} ft".format(marker))  # noqa: UP031, UP032
        ft_planted[ftrow.id] = marker
    res = tm_module.scrub_scaffold(session, apply=False, project_id=pa.id)
    for label, planted, hits in (
        ("segment", seg_planted, res["segments"]),
        ("segment_tm", tm_planted, res["segment_tm"]),
        ("full_text", ft_planted, res["full_text"]),
    ):
        caught = {h["id"] for h in hits}
        if caught != set(planted):
            _fail(
                "%s: scan missed %d marker(s) (hardcode drift?)"  # noqa: UP031
                % (label, len(set(planted) - caught))
            )
    _ok(
        "all %d scaffold.markers() tokens detected across segment + segment_tm + full_text"  # noqa: UP031
        % len(seg_planted)
    )


# ---------------------------------------------------------------------------
# B. The main scenario: plant the three poisoned-segment classes + TM + the spared rows.
# ---------------------------------------------------------------------------
def build_scenario(session):
    p = _proj(session, "scrubmain")
    p2 = _proj(session, "otherproj")
    doc = _doc(session, p.id, "doc09.md")
    doc2 = _doc(session, p2.id, "other.md")

    rows = {}
    # seg 11 class: poisoned, stored as CLEAN mt, zero flags.
    rows["clean_mt"] = _seg(
        session, doc.id, CLEAN_SRC, SEG11_ECHO, 0, status=SegStatus.MT, qa_flags=None
    )
    # segs 0/12 class: needs_review already, but WITHOUT the scaffold flag (predates gate).
    rows["review_noflag"] = _seg(
        session, doc.id, "Intro", STYLE_ECHO, 1, status=SegStatus.NEEDS_REVIEW, qa_flags=None
    )
    # already fully quarantined (needs_review + scaffold flag) -> idempotency target.
    rows["already"] = _seg(
        session,
        doc.id,
        "Intro two",
        STYLE_ECHO,
        2,
        status=SegStatus.NEEDS_REVIEW,
        qa_flags=[quality.scaffold_echo_flag(scaffold.SCAFFOLD_ECHO_REASON + ": prior")],
    )
    # clean translation -> never touched.
    rows["clean"] = _seg(session, doc.id, CLEAN_SRC, CLEAN_TGT, 3)
    # source carries the marker -> abstain-in-source spares it.
    rows["abstain"] = _seg(session, doc.id, ABSTAIN_SRC, ABSTAIN_TGT, 4)
    # poisoned but in ANOTHER project -> excluded from a p-scoped scan.
    rows["other_proj"] = _seg(session, doc2.id, CLEAN_SRC, SEG11_ECHO, 0)

    # TM: two poisoned (deleted), two spared (legit + abstain-in-source).
    rows["tm_bad"] = _tm(session, p.id, "seg style src", STYLE_ECHO)
    rows["ft_bad"] = _ft(session, p.id, "ft src", SEG11_ECHO)
    rows["tm_good"] = _tm(session, p.id, "legit src", "Una traduccion limpia sin marcador.")
    rows["tm_abstain"] = _tm(session, p.id, ABSTAIN_SRC, ABSTAIN_TGT)

    # a semantic embedding for the poisoned SegmentTM -> must be invalidated on delete.
    emb = SegmentEmbedding(
        segment_tm_id=rows["tm_bad"].id,
        project_id=p.id,
        source_locale="en-US",
        target_locale="es-ES",
        model="test",
        dim=1,
        embedding=b"\x00\x00\x80?",
    )
    session.add(emb)
    session.commit()
    return p, p2, rows


def check_dry_run(session, p, p2, rows):
    print("B. dry-run: reports the poison, spares legit rows, changes NOTHING")
    n_audit_before = len(
        session.exec(select(AuditLog).where(AuditLog.action == "data.scrub_scaffold")).all()
    )
    dry = tm_module.scrub_scaffold(session, apply=False, project_id=p.id)
    seg_ids = {h["id"] for h in dry["segments"]}
    want = {rows["clean_mt"].id, rows["review_noflag"].id, rows["already"].id}
    if seg_ids != want:
        _fail(
            "dry-run segment matches wrong: got %d, want 3 (clean_mt/review_noflag/already)"  # noqa: UP031
            % len(seg_ids)
        )
    # legit + abstain + other-project segments are NOT matched.
    for spared in ("clean", "abstain", "other_proj"):
        if rows[spared].id in seg_ids:
            _fail(f"dry-run must NOT match the {spared} segment")
    tm_ids = {h["id"] for h in dry["segment_tm"]}
    ft_ids = {h["id"] for h in dry["full_text"]}
    if tm_ids != {rows["tm_bad"].id}:
        _fail("dry-run SegmentTM matches wrong (legit/abstain must be spared)")
    if ft_ids != {rows["ft_bad"].id}:
        _fail("dry-run FullTextTM matches wrong")
    if dry["matched"] != 5:
        _fail("dry-run matched must be 5 (3 seg + 2 tm), got %d" % dry["matched"])  # noqa: UP031
    if dry["would_quarantine"] != 2:
        _fail(
            "would_quarantine must be 2 (already-flagged seg excluded), got %d"  # noqa: UP031
            % dry["would_quarantine"]
        )
    if dry["would_delete"] != 2:
        _fail("would_delete must be 2, got %d" % dry["would_delete"])  # noqa: UP031
    # the already-quarantined seg is flagged as such; the other two are not.
    amap = {h["id"]: h["already_quarantined"] for h in dry["segments"]}
    if not amap[rows["already"].id]:
        _fail("the already-quarantined seg must report already_quarantined=True")
    if amap[rows["clean_mt"].id] or amap[rows["review_noflag"].id]:
        _fail("an un-flagged poisoned seg must report already_quarantined=False")
    _ok("dry-run: 3 segs + 2 TM matched; legit/abstain/other-project spared; counts honest")

    # NOTHING changed on disk.
    session.refresh(rows["clean_mt"])
    if rows["clean_mt"].status != SegStatus.MT or rows["clean_mt"].qa_flags:
        _fail("dry-run must not mutate a segment")
    if session.get(SegmentTM, rows["tm_bad"].id) is None:
        _fail("dry-run must not delete a TM row")
    _ok("dry-run mutated nothing (segment status/flags intact, TM row present)")

    # a GLOBAL (unscoped) dry-run also sees the other-project poison.
    allrun = tm_module.scrub_scaffold(session, apply=False)
    if rows["other_proj"].id not in {h["id"] for h in allrun["segments"]}:
        _fail("a global scan must include the other-project poisoned segment")
    _ok("a global (project_id=None) scan reaches every project's poison")

    # a dry-run (scoped OR global) must write ZERO audit rows.
    n_audit_after = len(
        session.exec(select(AuditLog).where(AuditLog.action == "data.scrub_scaffold")).all()
    )
    if n_audit_after != n_audit_before:
        _fail("a dry-run must never write an audit row")
    _ok("a dry-run writes no audit row")


def check_apply(session, p, p2, rows):
    print("C. --apply: quarantine segments (text kept) + delete TM + invalidate + audit")
    n_audit_before = len(
        session.exec(select(AuditLog).where(AuditLog.action == "data.scrub_scaffold")).all()
    )
    applied = tm_module.scrub_scaffold(session, apply=True, actor="tester", project_id=p.id)
    if applied["quarantined"] != 2:
        _fail("apply must quarantine exactly 2 segments, got %d" % applied["quarantined"])  # noqa: UP031
    if applied["deleted"] != 2:
        _fail("apply must delete exactly 2 TM entries, got %d" % applied["deleted"])  # noqa: UP031

    # the seg-11 class: routed to needs_review, scaffold flag, TEXT UNCHANGED.
    session.refresh(rows["clean_mt"])
    s = rows["clean_mt"]
    if s.status != SegStatus.NEEDS_REVIEW:
        _fail("clean_mt must route to needs_review")
    if s.target_text != SEG11_ECHO:
        _fail("the remediation must NEVER rewrite/delete the segment target text")
    flags = _scaffold_flags(s)
    if len(flags) != 1:
        _fail("clean_mt must carry exactly one scaffold flag, got %d" % len(flags))  # noqa: UP031
    if flags[0].get("severity") != "major":
        _fail("the scaffold flag must be major")
    if flags[0].get("source") != "remediation":
        _fail("the flag must record source=remediation, got {!r}".format(flags[0].get("source")))
    _ok("seg-11 class: needs_review + one scaffold flag (source=remediation), text kept")

    # segs 0/12 class: was needs_review w/o flag -> now carries the flag.
    session.refresh(rows["review_noflag"])
    if (
        rows["review_noflag"].status != SegStatus.NEEDS_REVIEW
        or len(_scaffold_flags(rows["review_noflag"])) != 1
    ):
        _fail("review_noflag must gain the scaffold flag and stay needs_review")
    _ok("segs 0/12 class: prior needs_review row gains the scaffold flag")

    # already-quarantined seg: STILL exactly one flag (no duplication, no churn).
    session.refresh(rows["already"])
    if len(_scaffold_flags(rows["already"])) != 1:
        _fail("the already-quarantined seg must keep exactly ONE scaffold flag")
    _ok("already-quarantined seg stays at one flag (no duplication)")

    # clean + abstain segments untouched.
    for spared in ("clean", "abstain"):
        session.refresh(rows[spared])
        if rows[spared].status != SegStatus.MT or rows[spared].qa_flags:
            _fail(f"the {spared} segment must be left clean (mt, no flags)")
    # other-project poison untouched by a p-scoped apply.
    session.refresh(rows["other_proj"])
    if rows["other_proj"].status != SegStatus.MT or rows["other_proj"].qa_flags:
        _fail("a p-scoped apply must NOT touch the other-project segment")
    _ok("clean, abstain and other-project segments untouched")

    # poisoned TM deleted; legit + abstain TM survive.
    if session.get(SegmentTM, rows["tm_bad"].id) is not None:
        _fail("the poisoned SegmentTM must be deleted")
    if session.get(FullTextTM, rows["ft_bad"].id) is not None:
        _fail("the poisoned FullTextTM must be deleted")
    if (
        session.get(SegmentTM, rows["tm_good"].id) is None
        or session.get(SegmentTM, rows["tm_abstain"].id) is None
    ):
        _fail("a legit / abstain-in-source TM row must be SPARED")
    _ok("poisoned SegmentTM + FullTextTM deleted; legit + abstain TM spared")

    # semantic embedding invalidated for the deleted TM row.
    if (
        session.exec(
            select(SegmentEmbedding).where(SegmentEmbedding.segment_tm_id == rows["tm_bad"].id)
        ).first()
        is not None
    ):
        _fail("the deleted TM row's semantic embedding must be invalidated")
    _ok("the deleted TM row's semantic embedding is gone (no resurrection)")

    # exactly one new audit row, attributed to the actor.
    audit_rows = session.exec(
        select(AuditLog).where(AuditLog.action == "data.scrub_scaffold")
    ).all()
    if len(audit_rows) != n_audit_before + 1:
        _fail("apply must write exactly ONE audit row")
    if audit_rows[-1].actor != "tester":
        _fail("the audit row must record the actor")
    _ok("one audit row (action=data.scrub_scaffold) attributed to the actor")


def check_idempotent(session, p, rows):
    print("D. idempotency: a second --apply changes nothing and writes no no-op audit row")
    n_audit_before = len(
        session.exec(select(AuditLog).where(AuditLog.action == "data.scrub_scaffold")).all()
    )
    second = tm_module.scrub_scaffold(session, apply=True, actor="tester2", project_id=p.id)
    # TM is gone -> nothing to delete. Segments still MATCH (text unchanged) but are all
    # already quarantined now -> nothing to change.
    if second["deleted"] != 0:
        _fail("second apply must delete nothing, got %d" % second["deleted"])  # noqa: UP031
    if second["quarantined"] != 0:
        _fail("second apply must quarantine nothing, got %d" % second["quarantined"])  # noqa: UP031
    if second["would_quarantine"] != 0:
        _fail("second apply would_quarantine must be 0, got %d" % second["would_quarantine"])  # noqa: UP031
    if second["matched"] != 3:
        _fail("segments still match on re-scan (text kept): expected 3, got %d" % second["matched"])  # noqa: UP031
    n_audit_after = len(
        session.exec(select(AuditLog).where(AuditLog.action == "data.scrub_scaffold")).all()
    )
    if n_audit_after != n_audit_before:
        _fail("a no-op apply must NOT write an audit row")
    _ok("second apply: 0 changed, 3 still matched (text kept), no new audit row")


def check_empty_project_scope(session):
    print("E. scoping: a --project with NO documents scans NOTHING (never a global fallback)")
    # A poisoned segment exists somewhere globally...
    pglob = _proj(session, "hasdocs")
    dglob = _doc(session, pglob.id, "g.md")
    _seg(session, dglob.id, CLEAN_SRC, SEG11_ECHO, 0)
    # ...and an EMPTY project with zero documents. Its scan must match nothing -- the
    # `document_id IN (doc_ids or [-1])` guard must not degrade to an unscoped scan.
    empty = _proj(session, "nodocs")
    res = tm_module.scrub_scaffold(session, apply=False, project_id=empty.id)
    if res["segments"]:
        _fail(
            "an empty-project scope matched %d segment(s) -- it must match NONE"  # noqa: UP031
            % len(res["segments"])
        )
    _ok("an empty-project scope matches no segments (no unscoped fallback)")


def check_family_preservation(session):
    print("F. quarantine preserves OTHER qa_flag families (only nontranslation is replaced)")
    pf = _proj(session, "familyproj")
    df = _doc(session, pf.id, "f.md")
    dnt = {"type": "dnt_violation", "severity": "major", "message": "keep me"}
    seg = _seg(
        session, df.id, CLEAN_SRC, SEG11_ECHO, 0, status=SegStatus.NEEDS_REVIEW, qa_flags=[dnt]
    )
    tm_module.scrub_scaffold(session, apply=True, actor="tester", project_id=pf.id)
    session.refresh(seg)
    types = [f.get("type") for f in (seg.qa_flags or [])]
    if "dnt_violation" not in types:
        _fail("the pre-existing dnt_violation flag must survive the scaffold quarantine")
    if len(_scaffold_flags(seg)) != 1:
        _fail("the segment must also gain exactly one scaffold flag")
    _ok("a pre-existing dnt flag survives; the scaffold flag is added alongside it")


def main():
    print("verify_scaffold_remediation -- core-QA Part 1 scaffold-echo cleanup")
    init_db()
    with Session(engine) as session:
        check_marker_parity(session)
        p, p2, rows = build_scenario(session)
        check_dry_run(session, p, p2, rows)
        check_apply(session, p, p2, rows)
        check_idempotent(session, p, rows)
        check_empty_project_scope(session)
        check_family_preservation(session)
    print("verify_scaffold_remediation OK")


if __name__ == "__main__":
    main()
