"""Wave 2 Train A — post-edit capture + LQA attribution + the locale-filtered delete.

SPEC-wave2-qa-pe-reporting §9 "Train A". Hermetic + offline (throwaway LOC_TMS_DATA_DIR,
no provider keys; the LLM judge is monkeypatched where a live finding is needed).

Groups (spec test numbers in brackets):
  1  distance math + anti-fork equality with eval_metrics                        [1]
  2  episode model: both orders -> two rows; same-actor continuation             [2,3]
  3  baseline_kind matrix + cum_* NULL rules                                     [4]
  4  no-op / revert-not-captured / N1 lineage boundary                           [5,5b]
  5  N4 savepoint (capture failure never fails the caller) + N9 race retry       [5c,5d]
  6  seam 3 (_commit_body) captures full_text; revert path does not             [5e]
  7  LQA attribution: locale/source/judge at persist; judge provenance stripped  [6]
  8  scoring_id: one uuid per card; two segments differ; identical score/card    [7]
  9  locale-filtered pre-score delete + the regime filter                        [8]
  10 canonicalization of dimension/severity at persist                          [9]
  11 render_findings byte-identical with the new keys present                   [10]
  12 vendor attribution resolution rule                                         [11]
  13 build paths: table + six columns on both paths; _ADDED_COLUMNS matches     [12]
  14 lifecycle: the two INDEPENDENT bulk-delete sites leave zero orphans (N11)  [13]
  15 GDPR: erase_user tombstones SegmentPostEdit.actor                          [14]

Final line: "POSTEDIT OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_postedit_")

from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.db import _ADDED_COLUMNS, engine, init_db
from app.models import (
    Customer,
    Document,
    LQAResult,
    Project,
    Segment,
    SegmentPostEdit,
    SegStatus,
    StepType,
    TargetLocale,
    TransMode,
    User,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    Workflow,
    WorkflowRun,
)
from app.services import (
    content,
    eval_metrics,
    lqa,
    lqa_attribution,
    orchestrator,
    postedit,
    retention,
    vendor_dispatch,
    vendor_return,
    vendors,
)
from app.services import workflows as _wf

init_db()

SRC, TGT = "en-US", "es-ES"
_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _mk_doc(session, *, mode=TransMode.SEGMENTED):
    p = Project(name="p%d" % _seq(), source_locale=SRC, default_provider="mock")  # noqa: UP031
    session.add(p)
    session.commit()
    d = Document(project_id=p.id, filename="f%d.txt" % _seq(), fmt="txt", mode=mode)  # noqa: UP031
    session.add(d)
    session.commit()
    return p, d


def _mk_seg(
    session,
    doc,
    *,
    target="hola mundo",
    source="hello world",
    status=SegStatus.MT,
    tm_match_pct=None,
    lock_band=None,
    locale=TGT,
    order_index=0,
):
    s = Segment(
        document_id=doc.id,
        target_locale=locale,
        order_index=order_index,
        source_text=source,
        target_text=target,
        status=status,
        tm_match_pct=tm_match_pct,
        lock_band=lock_band,
    )
    session.add(s)
    session.commit()
    return s


def _pe_rows(session, seg_id):
    return session.exec(
        select(SegmentPostEdit)
        .where(SegmentPostEdit.segment_id == seg_id)
        .order_by(SegmentPostEdit.episode_seq)
    ).all()


def _mk_vendor_leg(session, doc):
    """A real Vendor + WorkflowRun + VendorDispatch so SegmentPostEdit's vendor_id /
    dispatch_id FKs (enforced on SQLite) are satisfiable. Returns (vendor_id, leg_id)."""
    v = Vendor(name="v%d" % _seq())  # noqa: UP031
    session.add(v)
    session.commit()
    wf = Workflow(name="wf%d" % _seq())  # noqa: UP031
    session.add(wf)
    session.commit()
    run = WorkflowRun(workflow_id=wf.id, document_id=doc.id, target_locale=TGT)
    session.add(run)
    session.commit()
    leg = VendorDispatch(
        vendor_id=v.id,
        workflow_run_id=run.id,
        target_locale=TGT,
        status=VendorDispatchStatus.RETURNED,
    )
    session.add(leg)
    session.commit()
    return v.id, leg.id


# ===========================================================================
# 1. distance math + the anti-fork pin
# ===========================================================================
def test_distances_math_and_antifork():
    assert postedit.distances("", "") == (0, 0, 0.0)
    assert postedit.distances("abc", "abc") == (0, 0, 0.0)
    c, w, pct = postedit.distances("hello", "hallo")
    assert c == 1 and w == 1 and pct == 20.0
    # ANTI-FORK PIN: char, word AND pct must equal eval_metrics' own scorers over the same
    # pair — the Lab and this report must never disagree about "edit distance" (L3 pins pct).
    for a, b in [("the cat sat", "the dog sat"), ("", "hi"), ("abcdef", "abXdef")]:
        char, word, pct = postedit.distances(a, b)
        assert char == int(eval_metrics._edit_char("", a, b, None).value), (a, b)
        assert word == int(eval_metrics._edit_word("", a, b, None).value), (a, b)
        assert pct == round(eval_metrics._edit_pct("", a, b, None).value, 4), (a, b, pct)


# ===========================================================================
# 2. episode model — both orders, and same-actor continuation
# ===========================================================================
def test_episode_two_actors_vendor_then_human():
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="raw mt text", status=SegStatus.MT)
        vid, did = _mk_vendor_leg(s, d)
        # vendor episode
        postedit.capture(
            s,
            segment=seg,
            pre="raw mt text",
            post="vendor edited text",
            actor_kind="vendor",
            vendor_id=vid,
            dispatch_id=did,
        )
        s.commit()
        seg.target_text = "vendor edited text"
        s.add(seg)
        s.commit()
        # in-house reviewer episode over the vendor's result
        postedit.capture(
            s,
            segment=seg,
            pre="vendor edited text",
            post="reviewer final text",
            actor_kind="human",
            actor="rev@x.com",
        )
        s.commit()
        rows = _pe_rows(s, seg.id)
        assert len(rows) == 2, "vendor + reviewer must be two episodes"
        v, h = rows
        assert v.actor_kind == "vendor" and v.vendor_id == vid and v.dispatch_id == did
        assert h.actor_kind == "human" and h.actor == "rev@x.com"
        # the reviewer's edit does NOT change the vendor row's own distance
        assert v.distance_char == postedit.distances("raw mt text", "vendor edited text")[0]
        assert h.distance_char == postedit.distances("vendor edited text", "reviewer final text")[0]
        # cum_* on the max-episode row measures MT->final
        assert h.cum_distance_char == postedit.distances("raw mt text", "reviewer final text")[0]


def test_episode_two_actors_human_then_machine():
    # the reverse order (a human save left the segment machine-writable, then a
    # vendor/machine write) — also two rows, correctly attributed.
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="mt text", status=SegStatus.MT)
        vid, did = _mk_vendor_leg(s, d)
        postedit.capture(
            s, segment=seg, pre="mt text", post="human text", actor_kind="human", actor="u@x.com"
        )
        s.commit()
        postedit.capture(
            s,
            segment=seg,
            pre="human text",
            post="machine redo",
            actor_kind="vendor",
            vendor_id=vid,
            dispatch_id=did,
        )
        s.commit()
        rows = _pe_rows(s, seg.id)
        assert len(rows) == 2
        assert rows[0].actor_kind == "human" and rows[1].actor_kind == "vendor"


def test_same_actor_continuation_is_one_episode():
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="base", status=SegStatus.MT)
        postedit.capture(
            s, segment=seg, pre="base", post="base v2", actor_kind="human", actor="u@x.com"
        )
        s.commit()
        postedit.capture(
            s, segment=seg, pre="base v2", post="base v3", actor_kind="human", actor="u@x.com"
        )
        s.commit()
        rows = _pe_rows(s, seg.id)
        assert len(rows) == 1, "same actor + unbroken lineage stays ONE episode"
        assert rows[0].edit_count == 2
        # episode-local distance is from its OWN baseline (base -> base v3)
        assert rows[0].distance_char == postedit.distances("base", "base v3")[0]


# ===========================================================================
# 3. baseline_kind matrix + cum_* NULL rules
# ===========================================================================
def test_baseline_kind_matrix():
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        # machine status -> 'mt'
        seg = _mk_seg(s, d, target="m", status=SegStatus.MT)
        r = postedit.capture(s, segment=seg, pre="m", post="mm", actor_kind="human", actor="u@x")
        assert r.baseline_kind == "mt" and r.cum_distance_char is not None
        # tm_match_pct -> 'tm'
        seg2 = _mk_seg(s, d, target="t", status=SegStatus.NEW, tm_match_pct=95, order_index=1)
        r2 = postedit.capture(s, segment=seg2, pre="t", post="tt", actor_kind="human", actor="u@x")
        assert r2.baseline_kind == "tm" and r2.cum_distance_char is not None
        # lock_band -> 'tm'
        seg3 = _mk_seg(s, d, target="l", status=SegStatus.NEW, lock_band="ice", order_index=2)
        r3 = postedit.capture(s, segment=seg3, pre="l", post="ll", actor_kind="human", actor="u@x")
        assert r3.baseline_kind == "tm"
        # pre-write EDITED/APPROVED -> 'human', cum NULL (no machine origin)
        seg4 = _mk_seg(s, d, target="h", status=SegStatus.EDITED, order_index=3)
        r4 = postedit.capture(s, segment=seg4, pre="h", post="hh", actor_kind="human", actor="u@x")
        assert r4.baseline_kind == "human"
        assert r4.cum_distance_char is None and r4.cum_distance_pct is None
        # empty pre -> 'none', ALL distances NULL
        seg5 = _mk_seg(s, d, target="", status=SegStatus.NEW, order_index=4)
        r5 = postedit.capture(
            s, segment=seg5, pre="", post="typed anew", actor_kind="human", actor="u@x"
        )
        assert r5.baseline_kind == "none"
        assert r5.distance_char is None and r5.distance_pct is None
        assert r5.cum_distance_char is None
        s.commit()


# ===========================================================================
# 4. no-op / revert-not-captured / N1 lineage boundary
# ===========================================================================
def test_noop_records_nothing():
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="same")
        assert (
            postedit.capture(
                s, segment=seg, pre="same", post="same", actor_kind="human", actor="u@x"
            )
            is None
        )
        s.commit()
        assert _pe_rows(s, seg.id) == []


def test_n1_machine_recommit_opens_new_episode():
    # actor A edits -> a machine re-commit lands (breaks lineage) -> actor A edits again.
    # TWO episodes; the first's cum_* is NOT silently anchored to a dead baseline.
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="mt0", status=SegStatus.MT)
        postedit.capture(s, segment=seg, pre="mt0", post="A1", actor_kind="human", actor="a@x")
        s.commit()
        # a machine re-commit through the human_edit bypass replaced the text under A —
        # A's next edit sees pre="MACHINE" which != the episode's final_sha ("A1").
        postedit.capture(s, segment=seg, pre="MACHINE", post="A2", actor_kind="human", actor="a@x")
        s.commit()
        rows = _pe_rows(s, seg.id)
        assert len(rows) == 2, "a broken lineage must open a fresh episode (N1)"
        assert rows[1].baseline_text == "MACHINE"
        assert rows[0].edit_count == 1


# ===========================================================================
# 5. N4 savepoint safety + N9 race retry
# ===========================================================================
def test_n4_capture_failure_never_fails_the_caller():
    # A capture that RAISES mid-transaction must roll back ONLY the savepoint; the
    # caller's own pending work must still commit (no PendingRollbackError).
    orig = postedit.distances
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="mt", status=SegStatus.MT)
        # pending caller work in the same transaction
        other = _mk_seg(s, d, target="untouched", order_index=9)
        other.target_text = "caller wrote this"
        s.add(other)
        seg_id, other_id = seg.id, other.id
        try:
            postedit.distances = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
            r = postedit.capture_guarded(
                s, segment=seg, pre="mt", post="edited", actor_kind="human", actor="u@x"
            )
            assert r is None, "a failed capture returns None, never raises"
        finally:
            postedit.distances = orig
        s.commit()  # must NOT raise
    with Session(engine) as s2:
        # the caller's write survived; the capture wrote nothing
        assert s2.get(Segment, other_id).target_text == "caller wrote this"
        assert _pe_rows(s2, seg_id) == []


def test_n9_race_retry_recovers():
    # Force an IntegrityError on the FIRST capture call; capture_guarded retries once in a
    # fresh savepoint and succeeds, and the session stays usable.
    orig = postedit.capture
    state = {"n": 0}

    def flaky(session, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("stmt", {}, Exception("UNIQUE"))
        return orig(session, **kwargs)

    with Session(engine) as s:
        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="mt", status=SegStatus.MT)
        try:
            postedit.capture = flaky
            r = postedit.capture_guarded(
                s, segment=seg, pre="mt", post="done", actor_kind="human", actor="u@x"
            )
        finally:
            postedit.capture = orig
        assert state["n"] == 2, "must have retried exactly once"
        assert r is not None and r.id is not None
        s.commit()  # session survived the IntegrityError
        assert len(_pe_rows(s, seg.id)) == 1


# ===========================================================================
# 6. seam 3 — _commit_body captures full_text; the revert path does not
# ===========================================================================
def test_seam3_commit_body_capture_flag():
    from app.services import fulltext_paragraphs as ftp

    with Session(engine) as s:
        _p, d = _mk_doc(s, mode=TransMode.FULL_TEXT)
        seg = _mk_seg(
            s,
            d,
            source="para one\n\npara two",
            target="orig one\n\norig two",
            status=SegStatus.EDITED,
        )
        # a SAVE (pe_capture=True) records a full_text episode
        ftp._commit_body(
            s,
            seg,
            ["new one", "orig two"],
            provenance="para-edit",
            pe_capture=True,
            pe_actor="ed@x.com",
        )
        s.commit()
        rows = _pe_rows(s, seg.id)
        assert len(rows) == 1
        assert rows[0].unit_kind == "full_text"
        assert rows[0].actor == "ed@x.com" and rows[0].actor_kind == "human"
        # a REVERT (pe_capture defaults False) records NOTHING
        n_before = len(_pe_rows(s, seg.id))
        ftp._commit_body(s, seg, ["para one", "orig two"], provenance="para-source-fallback")
        s.commit()
        assert len(_pe_rows(s, seg.id)) == n_before, "a revert is a discard, not captured"


def test_revert_then_edit_opens_fresh_episode():
    # §3.2.4: a revert leaves a stale episode, self-correcting because the next captured
    # edit sees sha(pre) != final_sha and opens a fresh episode.
    from app.services import fulltext_paragraphs as ftp

    with Session(engine) as s:
        _p, d = _mk_doc(s, mode=TransMode.FULL_TEXT)
        seg = _mk_seg(s, d, source="p1\n\np2", target="mt1\n\nmt2", status=SegStatus.MT)
        ftp._commit_body(
            s, seg, ["edit1", "mt2"], provenance="para-edit", pe_capture=True, pe_actor="u@x"
        )
        s.commit()
        assert len(_pe_rows(s, seg.id)) == 1
        # a REVERT changes the stored body but is NOT captured
        ftp._commit_body(s, seg, ["p1", "mt2"], provenance="para-source-fallback")
        s.commit()
        assert len(_pe_rows(s, seg.id)) == 1, "the revert recorded nothing"
        # the next captured SAVE sees pre != the episode's final_sha -> a fresh episode
        ftp._commit_body(
            s, seg, ["edit2", "mt2"], provenance="para-edit", pe_capture=True, pe_actor="u@x"
        )
        s.commit()
        assert len(_pe_rows(s, seg.id)) == 2, "a captured edit after a revert opens a new episode"


# ===========================================================================
# 7. LQA attribution at persist — locale/source/judge; judge provenance stripped
# ===========================================================================
class _Judge:
    """Force lqa's judge live and return a canned findings payload."""

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        self._orig = (lqa.provider_is_live, lqa.run_completion)
        lqa.provider_is_live = lambda name: True
        lqa.run_completion = lambda *a, **kw: {
            "text": self.payload,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "usage": {},
            "cost_usd": 0.0,
        }
        return self

    def __exit__(self, *exc):
        lqa.provider_is_live, lqa.run_completion = self._orig
        return False


def test_offline_card_source_is_structural_and_judge_null():
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        # a REQUIRE rulebook makes a deterministic offline rulebook finding
        lqa.save_rulebook(SRC, TGT, "REQUIRE zzzterm")
        _mk_seg(s, d, source="need the term", target="missing it here")
        lqa.score_document(s, d.id, TGT, persist=True)
        rows = s.exec(select(LQAResult).where(LQAResult.document_id == d.id)).all()
        assert rows, "the REQUIRE rule must produce a finding"
        for r in rows:
            assert r.finding_source in ("rulebook", "structural")
            assert r.judge is None, "offline card must persist NO phantom judge"
            assert r.target_locale == TGT


def test_llm_finding_source_and_judge_stamped_and_forgery_stripped():
    # A judge that forges its own provenance ("source":"human","judge":"x") AND an
    # `adjudicated` marker must have all three stripped; OUR source='llm' + provider/model
    # is stamped instead.
    payload = (
        '{"errors":[{"dimension":"Fluency","severity":"minor","note":"n",'
        '"source":"human","judge":"forged/model","adjudicated":true}]}'
    )
    with _Judge(payload):
        errs, *_ = lqa._llm_errors(
            "s", "t", SRC, TGT, "", "mock", spec=lqa.ProviderSpec(provider="mock", model="m")
        )
    assert len(errs) == 1
    e = errs[0]
    assert e["source"] == "llm", "judge-forged source must be replaced with 'llm'"
    assert e["judge"] == "mock/m", "our resolved provider/model, not the forged one"
    assert "adjudicated" not in e, "a judge-supplied adjudicated marker is stripped"


# ===========================================================================
# 8. scoring_id — one uuid per card, two segments differ, identical score per card
# ===========================================================================
def test_scoring_id_per_card():
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        lqa.save_rulebook(SRC, TGT, "REQUIRE alpha\nREQUIRE beta")
        _mk_seg(s, d, source="a", target="missing", order_index=0)
        _mk_seg(s, d, source="b", target="also missing", order_index=1)
        lqa.score_document(s, d.id, TGT, persist=True)
        rows = s.exec(select(LQAResult).where(LQAResult.document_id == d.id)).all()
        by_seg = {}
        for r in rows:
            assert r.scoring_id, "every persisted row carries a scoring_id"
            by_seg.setdefault(r.segment_id, set()).add(r.scoring_id)
        # each segment's rows share ONE scoring_id
        for seg_id, ids in by_seg.items():  # noqa: B007
            assert len(ids) == 1, "one card per segment == one scoring_id"
        # two different segments get DIFFERENT ids
        all_ids = {list(v)[0] for v in by_seg.values()}  # noqa: RUF015
        assert len(all_ids) == len(by_seg) >= 2
        # all rows of a card carry the identical score
        for seg_id in by_seg:
            scores = {r.score for r in rows if r.segment_id == seg_id}
            assert len(scores) == 1


# ===========================================================================
# 9. locale-filtered pre-score delete + the regime filter
# ===========================================================================
def test_locale_filtered_delete_and_regime_filter():
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        seg_es = _mk_seg(s, d, source="x", target="y", locale="es-ES", order_index=0)
        # pre-existing findings for a DIFFERENT real locale (fr-FR) — must SURVIVE
        s.add(
            LQAResult(
                document_id=d.id,
                segment_id=seg_es.id,
                dimension="Fluency",
                severity="minor",
                note="fr",
                target_locale="fr-FR",
            )
        )
        # a legacy NULL-locale row + a ""-locale row (both un-attributable) — must be CLEARED
        s.add(
            LQAResult(
                document_id=d.id,
                segment_id=seg_es.id,
                dimension="Fluency",
                severity="minor",
                note="legacy-null",
                target_locale=None,
            )
        )
        s.add(
            LQAResult(
                document_id=d.id,
                segment_id=seg_es.id,
                dimension="Fluency",
                severity="minor",
                note="legacy-empty",
                target_locale="",
            )
        )
        # a workflow-APPEND row (segment_id IS NULL) for es-ES — the regime filter must
        # KEEP it (score_document must stop destroying workflow history)
        s.add(
            LQAResult(
                document_id=d.id,
                segment_id=None,
                dimension="Accuracy",
                severity="major",
                note="workflow",
                target_locale="es-ES",
            )
        )
        s.commit()
        lqa.save_rulebook(SRC, "es-ES", "REQUIRE zzz")
        lqa.score_document(s, d.id, "es-ES", persist=True)
        rows = s.exec(select(LQAResult).where(LQAResult.document_id == d.id)).all()
        notes = {r.note for r in rows}
        locales = {r.target_locale for r in rows}
        assert "fr" in notes, "a different real locale's findings must survive"
        assert "legacy-null" not in notes and "legacy-empty" not in notes, (
            "un-attributable legacy rows are cleared on the first re-score"
        )
        assert "workflow" in notes, "the regime filter must keep segment_id IS NULL rows"
        # no post-Wave-2 segment-level row has a NULL target_locale
        for r in rows:
            if r.segment_id is not None and r.note not in ("fr",):
                assert r.target_locale is not None
        assert "es-ES" in locales and "fr-FR" in locales


# ===========================================================================
# 10. canonicalization of dimension/severity at persist
# ===========================================================================
def test_canonicalization_units():
    assert lqa.canon_severity(" MAJOR ") == "major"
    assert lqa.canon_severity("Neutral") == "neutral"
    assert lqa.canon_severity("blahsev") == "other"
    assert lqa.canon_dimension("accuracy/Omission") == "Accuracy/Omission"
    assert lqa.canon_dimension("Accuracy") == "Accuracy"
    unknown = lqa.canon_dimension("Frobnitz/foo")
    assert unknown.startswith("Other/")
    # a homoglyph / angle-bracket dimension is neutralized in the Other bucket
    neutral = lqa.canon_dimension("<script>alpha")
    assert "<" not in neutral and neutral.startswith("Other/")


def test_sanitize_axis_key_csv_and_html_guard():
    # M3: the report axis-key sanitizer Train B routes every key through (SPEC Errata E1).
    assert lqa.sanitize_axis_key("=cmd()") == "'=cmd()"  # CSV formula-injection guard
    for c in ("+", "-", "@"):
        assert lqa.sanitize_axis_key(c + "x").startswith("'" + c), c
    assert "<" not in lqa.sanitize_axis_key("<script>x")  # HTML/homoglyph neutralized
    assert lqa.sanitize_axis_key("z" * 500) == "z" * 64  # length-capped
    assert lqa.sanitize_axis_key("Accuracy/Omission") == "Accuracy/Omission"  # safe unchanged
    # L13: the cap must not slice an HTML entity mid-sequence — every '&' belongs to a whole
    # &lt;/&gt; (no dangling '&l' / '&lt').
    out = lqa.sanitize_axis_key("<" * 70)
    assert "&" not in out.replace("&lt;", "").replace("&gt;", ""), out


# ===========================================================================
# 11. render_findings byte-identical with the new keys present
# ===========================================================================
def test_render_findings_ignores_new_keys():
    base = {
        "score": 90,
        "gate": "auto_approved",
        "errors": [{"dimension": "Fluency", "severity": "minor", "note": "n"}],
    }
    withkeys = {
        "score": 90,
        "gate": "auto_approved",
        "errors": [
            {
                "dimension": "Fluency",
                "severity": "minor",
                "note": "n",
                "source": "llm",
                "judge": "mock/m",
            }
        ],
    }
    assert lqa.render_findings(base) == lqa.render_findings(withkeys)


# The EXACT judge prompt for a fixed input, captured from the code as it stands. The Wave 2
# source/judge tagging happens AFTER the judge responds (in the boundary strip), so it must
# NOT perturb prompt CONSTRUCTION. Pinned against this stored constant (NOT `git show HEAD`,
# which is structurally vacuous) so any drift in the judge prompt fails loudly.
#
# UPDATED DELIBERATELY for #163. The span request used to ride inside `_glossary_clause`, so
# this no-glossary prompt never carried it; it is now `lqa._SPAN_CLAUSE`, spliced on EVERY
# judge call, and the JSON shape names the field. That is the whole point of #163 -- the
# orchestrator attributes findings to segments with `span`, and a field only requested when
# approved terms happened to match was empty exactly where the correction loop needed it.
# This pin is the repo's only honest judge-prompt guard, so it is edited by hand, here, on
# purpose -- never by pasting whatever the code currently emits.
_EXPECTED_JUDGE_PROMPT = (
    "You are an LQA reviewer scoring an en-US->es-ES translation using the MQM error "
    "typology (dimensions: Accuracy, Fluency, Terminology, Locale convention, Style, "
    "Verity, Design, Internationalization; severities: minor, major, critical). Apply "
    "this language-pair rulebook:\n(none)\n\n"
    'Give EVERY error object a "span" key whose value is the EXACT substring of the '
    "translation that the error is about, copied verbatim and never paraphrased; omit "
    "the key only when no such substring exists.\n\n"
    "Source: Buy the widget.\nTranslation: Compra el widget.\n\n"
    'Return ONLY JSON: {"errors":[{"dimension":"...","severity":"...","note":"...",'
    '"span":"..."}]}'
)


def test_judge_prompt_byte_identical_pinned():
    cap = {}
    orig = (lqa.provider_is_live, lqa.run_completion)
    lqa.provider_is_live = lambda n: True

    def fake(spec, prompt, **kw):
        cap["p"] = prompt
        return {
            "text": '{"errors":[]}',
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "usage": {},
            "cost_usd": 0.0,
        }

    lqa.run_completion = fake
    try:
        lqa._llm_errors(
            "Buy the widget.",
            "Compra el widget.",
            "en-US",
            "es-ES",
            "",
            "mock",
            spec=lqa.ProviderSpec(provider="mock", model="m"),
        )
    finally:
        lqa.provider_is_live, lqa.run_completion = orig
    assert cap["p"] == _EXPECTED_JUDGE_PROMPT, "the judge prompt drifted"


def test_llm_judge_no_trailing_slash_when_model_none():
    # L11: judge renders 'provider', not 'provider/', when the model is unknown.
    payload = '{"errors":[{"dimension":"Fluency","severity":"minor","note":"n"}]}'
    with _Judge(payload):
        # force the spec's model to None
        errs, *_ = lqa._llm_errors(
            "s", "t", SRC, TGT, "", "mock", spec=lqa.ProviderSpec(provider="mock", model=None)
        )
    assert errs[0]["judge"] == "mock", errs[0]["judge"]


# ===========================================================================
# 12. vendor attribution resolution rule
# ===========================================================================
def _mk_run(session, doc):
    wf = Workflow(name="wf%d" % _seq())  # noqa: UP031
    session.add(wf)
    session.commit()
    run = WorkflowRun(workflow_id=wf.id, document_id=doc.id, target_locale=TGT)
    session.add(run)
    session.commit()
    return run


def _mk_leg(session, *, run_id, locale, status, returned_at, vendor_id):
    leg = VendorDispatch(
        vendor_id=vendor_id,
        workflow_run_id=run_id,
        target_locale=locale,
        status=status,
        returned_at=returned_at,
    )
    session.add(leg)
    session.commit()
    return leg


def test_vendor_attribution_rule():
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        v = Vendor(name="v%d" % _seq())  # noqa: UP031
        s.add(v)
        s.commit()
        run_a, run_b = _mk_run(s, d), _mk_run(s, d)
        t0 = datetime(2026, 1, 1)
        # ad-hoc path (no run) -> always (None, None)
        assert lqa_attribution.vendor_for_scored_content(
            s, workflow_run_id=None, target_locale=TGT, scored_at=t0
        ) == (None, None)
        # a non-RETURNED leg never attributes
        _mk_leg(
            s,
            run_id=run_a.id,
            locale=TGT,
            status=VendorDispatchStatus.DISPATCHED,
            returned_at=t0,
            vendor_id=v.id,
        )
        assert lqa_attribution.vendor_for_scored_content(
            s, workflow_run_id=run_a.id, target_locale=TGT, scored_at=t0 + timedelta(days=1)
        ) == (None, None)
        # two RETURNED legs (a rework): pick the latest returned_at at-or-before scored_at
        l1 = _mk_leg(
            s,
            run_id=run_b.id,
            locale=TGT,
            status=VendorDispatchStatus.RETURNED,
            returned_at=t0,
            vendor_id=v.id,
        )
        l2 = _mk_leg(
            s,
            run_id=run_b.id,
            locale=TGT,
            status=VendorDispatchStatus.RETURNED,
            returned_at=t0 + timedelta(days=2),
            vendor_id=v.id,
        )
        vid, did = lqa_attribution.vendor_for_scored_content(
            s, workflow_run_id=run_b.id, target_locale=TGT, scored_at=t0 + timedelta(days=3)
        )
        assert (vid, did) == (v.id, l2.id)
        # a leg returning AFTER the card is not attributed
        _vid2, did2 = lqa_attribution.vendor_for_scored_content(
            s, workflow_run_id=run_b.id, target_locale=TGT, scored_at=t0 + timedelta(days=1)
        )
        assert did2 == l1.id, "only the leg returned at-or-before scored_at attributes"


# ===========================================================================
# 13. build paths — table + lqaresult shim columns + _ADDED_COLUMNS agreement
# ===========================================================================
def test_build_paths_and_shim():
    from sqlalchemy import inspect as _inspect

    insp = _inspect(engine)
    assert "segmentpostedit" in insp.get_table_names()
    lqa_cols = {c["name"] for c in insp.get_columns("lqaresult")}
    # the Wave-2 six + the Wave-3 two (actor + withdrawn_at, SPEC-wave3-collaboration §6.1)
    for col in (
        "target_locale",
        "finding_source",
        "judge",
        "scoring_id",
        "vendor_id",
        "dispatch_id",
        "actor",
        "withdrawn_at",
    ):
        assert col in lqa_cols, "missing lqaresult column on the create_all path: " + col
    # _ADDED_COLUMNS["lqaresult"] names EXACTLY those eight migration columns
    shim = {name for name, _type in _ADDED_COLUMNS["lqaresult"]}
    assert shim == {
        "target_locale",
        "finding_source",
        "judge",
        "scoring_id",
        "vendor_id",
        "dispatch_id",
        "actor",
        "withdrawn_at",
    }
    # SegmentPostEdit is a NEW TABLE — it must NOT have a shim entry
    assert "segmentpostedit" not in _ADDED_COLUMNS


# ===========================================================================
# 14. lifecycle — the two INDEPENDENT bulk-delete sites (N11)
# ===========================================================================
def _seed_pe(session, doc, seg):
    postedit.capture(
        session,
        segment=seg,
        pre=seg.target_text or "x",
        post="edited once",
        actor_kind="human",
        actor="del@x.com",
    )
    session.commit()


def test_delete_previews_count_pe_rows():
    # §9 test 13: both destructive-action previews must count PE rows so they don't
    # understate what a delete removes.
    with Session(engine) as s:
        p, d = _mk_doc(s)
        s.add(TargetLocale(project_id=p.id, locale="es-ES"))
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        seg = _mk_seg(s, d, target="mt", locale="es-ES")
        _seed_pe(s, d, seg)  # one PE row
        prev = content.delete_project_preview(s, p.id)
        assert prev["cascade"]["segment_post_edits"] == 1, prev["cascade"]
        lprev = content.remove_locale_preview(s, p.id, "es-ES")
        assert lprev["deletes"]["segment_post_edits"] == 1, lprev["deletes"]


def test_lifecycle_retention_delete_document():
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="mt")
        _seed_pe(s, d, seg)
        assert _pe_rows(s, seg.id), "precondition: a PE row exists"
        retention.delete_document(s, d.id)
        assert (
            s.exec(select(SegmentPostEdit).where(SegmentPostEdit.document_id == d.id)).all() == []
        ), "retention.delete_document must leave zero orphan PE rows"


def test_lifecycle_remove_target_locale_independent_site():
    # content.remove_target_locale does its OWN sa_delete(Segment) — the independent site
    # N11 insists be exercised directly, not via the shared retention impl.
    with Session(engine) as s:
        p, d = _mk_doc(s)
        # two registered locales so es-ES is not the LAST (which would block removal)
        s.add(TargetLocale(project_id=p.id, locale="es-ES"))
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        seg = _mk_seg(s, d, target="mt", locale="es-ES")
        _seed_pe(s, d, seg)  # a PE row exists for this segment
        # clear the target so es-ES reads UNTRANSLATED (the permanence rule otherwise
        # refuses removing a translated locale); the PE row remains as an orphan risk.
        seg.target_text = ""
        s.add(seg)
        s.commit()
        assert _pe_rows(s, seg.id), "precondition: a PE row still exists"
        res = content.remove_target_locale(s, p.id, "es-ES")
        assert res is not None and not res.get("blocked"), res
        assert (
            s.exec(select(SegmentPostEdit).where(SegmentPostEdit.segment_id == seg.id)).all() == []
        ), "remove_target_locale must clear its own segments' PE rows"


def test_lifecycle_delete_project_and_document_wrapper():
    # The two DELEGATING sites (content.delete_project + delete_document) must also leave
    # no orphan PE rows — they reap segments via retention.delete_document.
    with Session(engine) as s:
        from app.services import content as _c

        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="mt")
        _seed_pe(s, d, seg)
        _c.delete_document(s, d.id)
        assert (
            s.exec(select(SegmentPostEdit).where(SegmentPostEdit.document_id == d.id)).all() == []
        )
        # and delete_project
        p2, d2 = _mk_doc(s)
        seg2 = _mk_seg(s, d2, target="mt2")
        _seed_pe(s, d2, seg2)
        _c.delete_project(s, p2.id)
        assert (
            s.exec(select(SegmentPostEdit).where(SegmentPostEdit.document_id == d2.id)).all() == []
        )


# ===========================================================================
# 15. GDPR — erase_user tombstones SegmentPostEdit.actor
# ===========================================================================
def test_gdpr_tombstones_actor():
    from app.services import gdpr

    email = "erase-me-%d@x.com" % _seq()  # noqa: UP031
    with Session(engine) as s:
        u = User(email=email)
        s.add(u)
        s.commit()
        uid = u.id
        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="mt")
        postedit.capture(s, segment=seg, pre="mt", post="edited", actor_kind="human", actor=email)
        s.commit()
    gdpr.erase_user(uid, actor="admin")
    with Session(engine) as s:
        rows = s.exec(select(SegmentPostEdit).where(SegmentPostEdit.actor == email)).all()
        assert rows == [], "the erased email must appear nowhere in SegmentPostEdit.actor"


# ===========================================================================
# REAL-FLOW SEAM TESTS (gate M1) — drive the actual callers, not the engine alone
# ===========================================================================

import httpx

_V_SOURCES = ["Hello world.", "The cat sat.", "Please sign in."]
_V_GOOD = ["Hola mundo del proveedor.", "El gato se sento aqui.", "Por favor inicie sesion ya."]


def _vmock_transport():
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


def _vseed():
    """A real customer+project+doc+segments+vendor+2-step workflow (the vendor-return
    fixture, from verify_vendor_return._seed)."""
    with Session(engine) as s:
        cust = Customer(name="C-%d" % _seq())  # noqa: UP031
        s.add(cust)
        s.commit()
        s.refresh(cust)
        p = Project(
            name="P-%d" % _seq(),  # noqa: UP031
            source_locale="en-US",
            default_provider="mock",
            customer_id=cust.id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, src in enumerate(_V_SOURCES):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text=src,
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        v = Vendor(
            name="V-%d" % _seq(),  # noqa: UP031
            endpoint_url="https://vendor.example.com/intake",
            rate_card={"currency": "USD", "bands": {"new": 0.2}},
        )
        v.secret_enc = vendors._enc_secret({"signing_secret": "sek"})
        s.add(v)
        s.commit()
        s.refresh(v)
        vendors.link_customer(s, v.id, cust.id)
        w = Workflow(
            name="W-%d" % _seq(),  # noqa: UP031
            scope="global",
            source_locale="en-US",
            target_locale="es-ES",  # noqa: RUF100, UP031
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        _wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        _wf.add_step(
            s,
            w.id,
            name="Vendor review",
            step_type=StepType.EXTERNAL,
            vendor_id=v.id,
            external_wait_hours=72,
        )
        return {"doc_id": d.id, "wf_id": w.id, "vendor_id": v.id}


def _vdispatch(env):
    vendor_dispatch.set_test_transport(_vmock_transport())
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, env["wf_id"], env["doc_id"], "es-ES"))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == env["wf_id"])).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        return run.id, leg.id, leg.claim_token


def _vseg_ids(doc_id):
    with Session(engine) as s:
        return [sg.id for sg in orchestrator._load_segments(s, doc_id, "es-ES")]


def _vpayload(ids, targets, *, claim):
    return {
        "contract_version": "1.0",
        "target_locale": "es-ES",
        "segments": [{"id": i, "target": t} for i, t in zip(ids, targets, strict=False)],
        "claim_token": claim,
    }


def test_seam2_real_vendor_flow_baselines_on_the_parked_mt():
    # Gate H1 REGRESSION: through the REAL run->dispatch->admit flow, every admitted vendor
    # edit must baseline on the parked MT (slots[pos]), NOT the stored segment (empty in the
    # native flow). A regression to seg.target_text lands baseline_kind='none' + NULL
    # distances and empties the whole vendor edit-distance deliverable.
    env = _vseed()
    _run_id, _leg_id, claim = _vdispatch(env)
    ids = _vseg_ids(env["doc_id"])
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s, _vpayload(ids, _V_GOOD, claim=claim), resume="sync", actor="tester"
        )
    assert out["complete"] and len(out["admitted"]) == 3, out
    with Session(engine) as s:
        rows = s.exec(select(SegmentPostEdit).where(SegmentPostEdit.segment_id.in_(ids))).all()
        assert len(rows) == 3, "one PE episode per admitted vendor unit"
        for r in rows:
            assert r.actor_kind == "vendor" and r.dispatch_id is not None
            assert r.baseline_kind != "none", (
                "H1: the vendor baseline must be the parked MT, not an empty stored target"
            )
            assert r.distance_char is not None and r.distance_char > 0
            assert r.baseline_chars > 0


def test_seam2_capture_failure_lets_admission_commit():
    # Gate 5c (vendor): force the capture to RAISE inside the real admission and assert the
    # admission still COMMITS (leg RETURNED, run resumes) — not merely "no exception escaped".
    env = _vseed()
    _run_id, leg_id, claim = _vdispatch(env)
    ids = _vseg_ids(env["doc_id"])
    orig = postedit.distances
    try:
        postedit.distances = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        with Session(engine) as s:
            out = vendor_return.admit_manual(
                s, _vpayload(ids, _V_GOOD, claim=claim), resume="sync", actor="tester"
            )
    finally:
        postedit.distances = orig
    assert out["complete"] and len(out["admitted"]) == 3, out
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED, "the admission must have committed"
        # the capture was rolled back by its savepoint -> no PE rows for these segments
        assert (
            s.exec(select(SegmentPostEdit).where(SegmentPostEdit.segment_id.in_(ids))).all() == []
        )


def test_seam2_source_echo_skips_capture():
    # Gate 5: a vendor that returns the SOURCE unchanged (a source-echo no-op) did no
    # post-editing — no PE row is written for that unit.
    env = _vseed()
    _run_id, _leg_id, claim = _vdispatch(env)
    ids = _vseg_ids(env["doc_id"])
    # unit 0 echoes its source; units 1-2 are real edits
    echoed = [_V_SOURCES[0], _V_GOOD[1], _V_GOOD[2]]
    with Session(engine) as s:
        vendor_return.admit_manual(
            s, _vpayload(ids, echoed, claim=claim), resume="sync", actor="tester"
        )
    with Session(engine) as s:
        rows = s.exec(select(SegmentPostEdit).where(SegmentPostEdit.segment_id.in_(ids))).all()
        seg_ids_with_pe = {r.segment_id for r in rows}
        assert ids[0] not in seg_ids_with_pe, "a source-echo unit records no post-edit"
        assert ids[1] in seg_ids_with_pe and ids[2] in seg_ids_with_pe


def test_seam1_real_segment_save_route_captures():
    # Gate 5c (segment save) + seam-1 wiring through the REAL web route. Operator mode (no
    # login wall) -> a bare TestClient POST returns 200.
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="mt text here", status=SegStatus.MT)
        seg_id = seg.id
    r = client.post(
        "/segments/%d/save" % seg_id,  # noqa: UP031
        data={"target_text": "human edited text", "tte_ms": "500"},  # noqa: RUF100, UP031
    )
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        rows = _pe_rows(s, seg_id)
        assert len(rows) == 1 and rows[0].actor_kind == "human"
        assert rows[0].distance_char is not None and rows[0].distance_char > 0
    # capture failure must not 500 the save (invariant 6 through the real route)
    orig = postedit.distances
    try:
        postedit.distances = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        r2 = client.post(
            "/segments/%d/save" % seg_id,  # noqa: UP031
            data={"target_text": "human edited text v2", "tte_ms": "10"},
        )
    finally:
        postedit.distances = orig
    assert r2.status_code == 200, "a capture failure must not fail the committed save"
    with Session(engine) as s:
        assert s.get(Segment, seg_id).target_text == "human edited text v2", (
            "the save itself committed despite the capture failure"
        )


def test_seam1_pe_commit_shed_on_persistent_lock_survives_the_save():
    # Gate M6: force a PERSISTENT writer-lock on the seam-1 PE commit through the REAL save
    # route. The request must still 200, the save must persist, the PE row is shed, the WARN
    # fires, and NO exception escapes. This PINS H2 + M5: with a bare `session.commit()` (no
    # commit_or_shed) the lock propagates and the route 500s — the revert-probe.
    import logging as _logging

    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as _SASession

    from app import db as _db
    from app.main import app

    client = TestClient(app)
    with Session(engine) as s:
        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="mt base", status=SegStatus.MT)
        seg_id = seg.id

    records = []
    handler = _logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    _logging.getLogger("loc_tms").addHandler(handler)

    orig_commit = _SASession.commit
    orig_sleep = _db.time.sleep

    def locking_commit(self, *a, **k):
        # Lock ONLY the PE commit — the segment save (:1057) and the llmemory commit run
        # with no SegmentPostEdit in the session yet, so they commit normally.
        if any(isinstance(o, SegmentPostEdit) for o in self):
            raise OperationalError(
                "INSERT INTO segmentpostedit", {}, Exception("database is locked")
            )
        return orig_commit(self, *a, **k)

    try:
        _SASession.commit = locking_commit
        _db.time.sleep = lambda *_a, **_k: None  # no backoff sleeps in the test
        r = client.post(
            "/segments/%d/save" % seg_id,  # noqa: UP031
            data={"target_text": "human edited under lock", "tte_ms": "5"},
        )
    finally:
        _SASession.commit = orig_commit
        _db.time.sleep = orig_sleep
        _logging.getLogger("loc_tms").removeHandler(handler)

    # Gate-required assertions: 200 + save persisted + WARN fired + no exception escaped.
    # (Whether the PE row itself survives is deliberately NOT asserted — the savepoint can
    # persist it even when the outer commit sheds, which is exactly why the WARN says "may
    # not have persisted", L12. Reverting to a bare session.commit() makes this 500 and
    # emits no WARN — the revert-probe.)
    assert r.status_code == 200, ("the request must survive a shed PE commit", r.status_code)
    with Session(engine) as s:
        assert s.get(Segment, seg_id).target_text == "human edited under lock", (
            "the save itself must persist despite the PE-commit lock"
        )
    assert any("PE capture may not have persisted" in m for m in records), (
        "the shed WARN must fire",
        records,
    )


def _ft_doc(session):
    """A FULL_TEXT document + its single whole-body Segment (2 paragraphs)."""
    p = Project(name="ftp%d" % _seq(), source_locale="en-US", default_provider="mock")  # noqa: UP031
    session.add(p)
    session.commit()
    d = Document(
        project_id=p.id,
        filename="ft%d.docx" % _seq(),  # noqa: UP031
        fmt="docx",
        mode=TransMode.FULL_TEXT,
        skeleton={"units": [{"text": "Para one."}, {"text": "Para two."}], "seg_locale": "en-US"},
    )
    session.add(d)
    session.commit()
    seg = Segment(
        document_id=d.id,
        target_locale="es-ES",
        order_index=0,
        source_text="Para one.\n\nPara two.",
        target_text="Uno mt.\n\nDos mt.",
        status=SegStatus.MT,
    )
    session.add(seg)
    session.commit()
    return p, d, seg


def test_seam3_web_api_cli_paragraph_save_all_capture():
    # Gate 5e: a full-text paragraph save captures unit_kind='full_text' on ALL THREE
    # surfaces (web route, /api route, CLI verb) — the headless-capable seam. Paragraph
    # writes are CAN_WRITE-gated (unlock-rbac), so web is driven through a REAL login
    # session (+ CSRF) and /api through a REAL write-role token; the CLI is the in-process
    # trusted operator (headless).
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Role
    from app.services import apikeys, security, settings
    from tests._csrf import csrf_for

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        pw = "fixture-pass-123"
        u = security.create_user("pe-admin-%d@x.com" % _seq(), "PE Admin", Role.ADMIN, pw)  # noqa: UP031
        _row, token = apikeys.create_key(u.id, "pe-key-%d" % _seq(), scopes="api docs")  # noqa: UP031
        client = TestClient(app)
        # establish a real web session
        client.get("/auth/login")
        csrf_for(client)
        client.post("/auth/login", data={"email": u.email, "password": pw})
        csrf_for(client)

        # (a) WEB — POST /documents/{id}/paragraphs/{index}/save (session cookie + CSRF)
        with Session(engine) as s:
            _p, d, seg = _ft_doc(s)
            web_doc, web_seg = d.id, seg.id
        r = client.post(
            "/documents/%d/paragraphs/0/save" % web_doc,  # noqa: UP031
            data={"locale": "es-ES", "text": "Uno EDITADO web."},
        )
        assert r.status_code == 200 and r.json().get("ok"), ("web", r.status_code, r.text)
        with Session(engine) as s:
            rows = _pe_rows(s, web_seg)
            assert len(rows) == 1 and rows[0].unit_kind == "full_text", ("web", rows)

        # (b) /api — POST /api/documents/{id}/paragraphs/{index}/save (write-role token)
        with Session(engine) as s:
            _p, d, seg = _ft_doc(s)
            api_doc, api_seg = d.id, seg.id
        r = client.post(
            "/api/documents/%d/paragraphs/0/save?locale=es-ES" % api_doc,  # noqa: UP031
            json={"text": "Uno EDITADO api."},
            headers={"X-Loc-Token": token},
        )
        assert r.status_code == 200 and r.json().get("ok"), ("api", r.status_code, r.text)
        with Session(engine) as s:
            rows = _pe_rows(s, api_seg)
            assert len(rows) == 1 and rows[0].unit_kind == "full_text", ("api", rows)
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()

    # (c) CLI — doc set-paragraph (in-process trusted operator, headless)
    from tests._cli_runner import run_cli

    with Session(engine) as s:
        _p, d, seg = _ft_doc(s)
        cli_doc, cli_seg = d.id, seg.id
    code, out, err = run_cli(
        [
            "doc",
            "set-paragraph",
            "--doc",
            str(cli_doc),
            "--locale",
            "es-ES",
            "--index",
            "0",
            "--text",
            "Uno EDITADO cli.",
        ]
    )
    assert code == 0, (out, err)
    with Session(engine) as s:
        rows = _pe_rows(s, cli_seg)
        assert len(rows) == 1 and rows[0].unit_kind == "full_text", ("cli", rows)


def test_pe_unique_index_rejects_duplicate_at_flush():
    # Gate 5d (part 1): the UNIQUE(segment_id, episode_seq) index is REAL and fires a
    # genuine IntegrityError AT FLUSH — not a synthetic Python raise. (A true concurrent
    # cross-connection race deadlocks on SQLite's single-writer model with capture's
    # savepoint, so the constraint is proven at the DB layer here and the retry logic
    # deterministically in test_n9_race_retry_recovers.)
    from sqlalchemy.exc import IntegrityError

    with Session(engine) as s:
        _p, d = _mk_doc(s)
        seg = _mk_seg(s, d, target="mt")
        s.add(
            SegmentPostEdit(
                segment_id=seg.id,
                document_id=d.id,
                episode_seq=1,
                actor_kind="human",
                actor="a",
                baseline_text="x",
                baseline_kind="mt",
                final_sha="h1",
            )
        )
        s.add(
            SegmentPostEdit(
                segment_id=seg.id,
                document_id=d.id,
                episode_seq=1,
                actor_kind="human",
                actor="b",
                baseline_text="y",
                baseline_kind="mt",
                final_sha="h2",
            )
        )
        raised = False
        try:
            s.flush()
        except IntegrityError:
            raised = True
            s.rollback()
        assert raised, "the (segment_id, episode_seq) UNIQUE index did not reject a duplicate"


def main():
    print("Wave 2 Train A — post-edit capture + LQA attribution")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("  -", name)
            fn()
    print("POSTEDIT OK")


if __name__ == "__main__":
    main()
