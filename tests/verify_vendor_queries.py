"""Wave 3 Train E — the buyer <-> vendor query round trip on the app side (SPEC §7).

Hermetic + offline: dispatch rides an injected httpx.MockTransport so no byte reaches a
network. This suite attacks the round trip rather than demonstrating it:

  [35]  OUTBOUND carry: two open threads ship, a closed one and an other-locale one do NOT,
        NO author email is in the serialized package, over-cap declares queries_truncated +
        queries_omitted with the right counts.
  [36]  RETURN admission: reply_to appends + flips ANSWERED; no reply_to opens a new thread
        assigned to the paused step's assignee; an over-cap / malformed `comments` block
        REFUSES the whole return with the leg untouched.
  [37]  N4 transaction law: record_vendor_post raising inside admission still lets the outer
        COMMIT succeed, the segments are written, a WARN is logged.
  [37b] Placement (MED-2): a comment on a hard-locked, status-protected and source-echo
        segment is recorded in all three cases; the post-edit capture does NOT fire for them.
  [38]  Idempotency (MED-4): re-admitting the same return creates no duplicate posts and, for
        a no-reply_to comment, no duplicate THREADS.
  [38b] Cap breach (MED-5): an over-full thread + an invalid reply_to -> comments_rejected,
        the return admits; a malformed `comments` still refuses the whole return.
  [38c] Kill switch (§5.2): queries_enabled=0 -> the package has NO `queries` key (asserted on
        bytes), and a return carrying comments admits with the comments silently skipped.
  [39]  reply_to scoping (HIGH): a reply_to on another document / locale / customer is refused,
        never an existence oracle.
  [40]  XLIFF: an outbound package renders per-unit <note from="loc-studio-query">; a return
        XLIFF carrying <note> admits nothing; a trans-unit <note from="loc-studio-claim"> is
        still ignored (anti-planting).

NOTE ON PRE-FIX PROOFS: every test here covers NEW Wave 3 behaviour, so none is a
fail-before proof of a pre-existing defect and none pins a commit sha.
"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_vendorq_")

import httpx
from sqlalchemy import func
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Project,
    QueryPost,
    QueryStatus,
    QueryThread,
    Role,
    RunStatus,
    Segment,
    SegmentPostEdit,
    SegStatus,
    StepType,
    TransMode,
    User,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    access,
    orchestrator,
    query_threads,
    vendor_dispatch,
    vendor_return,
    vendor_xliff,
    vendors,
)
from app.services import workflows as wf

init_db()

SIGNING = "sig-secret-q"
SOURCES = ["Hello world.", "The cat sat.", "You have mail."]


_SENT = []  # every request body actually put on the (mock) wire — for real-bytes assertions


def _mock_transport():
    def handler(request):
        try:  # noqa: SIM105
            _SENT.append(bytes(request.content))
        except Exception:
            pass
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


class _WarnCatcher(logging.Handler):
    """Capture WARN+ records from a named logger so a test can ASSERT the WARN fired (L1)."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


_SEQ = [0]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _seed(*, assignee="", assignee_is_real=True):
    """A customer + project + doc + 3 segments + a linked vendor + a workflow whose EXTERNAL
    step optionally names an assignee (so a vendor-opened thread lands in a worklist).

    Issue #51 (vendor path): the vendor writer now VALIDATES the step assignee, so a
    positive control must name a REAL active user who can see the project. When `assignee`
    is a plain address and `assignee_is_real`, seed that user + a project membership so the
    assignment resolves; pass assignee_is_real=False to leave it an unresolvable address
    (the black-hole / mail-relay negative)."""
    with Session(engine) as s:
        cust = Customer(name=f"C-{_seq()}")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        p = Project(
            name=f"P-{_seq()}", source_locale="en-US", default_provider="mock", customer_id=cust.id
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        if assignee and assignee_is_real:
            u = User(email=assignee, name="Rev", role=Role.REVIEWER, active=True)
            s.add(u)
            s.commit()
            access.add_member(s, p.id, assignee, added_by="seed")
            s.commit()
        d = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, src in enumerate(SOURCES):
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
            name=f"V-{_seq()}",
            endpoint_url="https://vendor.example.com/intake",
            rate_card={"currency": "USD", "bands": {"new": 0.2}},
        )
        v.secret_enc = vendors._enc_secret({"signing_secret": SIGNING})
        s.add(v)
        s.commit()
        s.refresh(v)
        vendors.link_customer(s, v.id, cust.id)
        w = Workflow(
            name=f"W-{_seq()}", scope="global", source_locale="en-US", target_locale="es-ES"
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(
            s,
            w.id,
            name="Vendor review",
            step_type=StepType.EXTERNAL,
            vendor_id=v.id,
            external_wait_hours=72,
            assignee_email=assignee or "",
        )
        return {
            "customer_id": cust.id,
            "project_id": p.id,
            "doc_id": d.id,
            "vendor_id": v.id,
            "wf_id": w.id,
        }


def _dispatch(env, locale="es-ES"):
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, env["wf_id"], env["doc_id"], locale))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == env["wf_id"])).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        assert leg is not None and leg.status == VendorDispatchStatus.DISPATCHED
        assert run.status == RunStatus.PAUSED
        return run.id, leg.id, leg.claim_token


def _seg_ids(doc_id, locale="es-ES"):
    with Session(engine) as s:
        return [sg.id for sg in orchestrator._load_segments(s, doc_id, locale)]


def _open_thread(*, segment_id, subject, body, author="buyer@x.test"):
    with Session(engine) as s:
        scope = query_threads.resolve_scope(s, segment_id=segment_id)
        t = query_threads.open_thread(
            s,
            user=None,
            scope=scope,
            subject=subject,
            body=body,
            actor=author,
            trusted_operator=True,
        )
        return t.id


def _payload(ids, targets, *, claim, comments_by_id=None, locale="es-ES"):
    comments_by_id = comments_by_id or {}
    segs = []
    for i, t in zip(ids, targets, strict=False):
        seg = {"id": i, "target": t}
        if i in comments_by_id:
            seg["comments"] = comments_by_id[i]
        segs.append(seg)
    return {
        "contract_version": "1.0",
        "target_locale": locale,
        "claim_token": claim,
        "segments": segs,
    }


# ============================================================================
# [35] OUTBOUND carry
# ============================================================================
def test_outbound_carry_filters_and_declares_truncation():
    env = _seed()
    ids = _seg_ids(env["doc_id"])
    # seg0: two OPEN threads (both ship) + a CLOSED one (not shipped).
    t_a = _open_thread(
        segment_id=ids[0], subject="Tag?", body="keep the tag please", author="ana@buyer.test"
    )
    t_b = _open_thread(
        segment_id=ids[0], subject="Tone?", body="formal tone", author="ben@buyer.test"
    )
    t_c = _open_thread(segment_id=ids[0], subject="Old", body="ignore", author="cara@buyer.test")
    with Session(engine) as s:
        query_threads.set_status(
            s, user=None, thread_id=t_c, status=QueryStatus.CLOSED, trusted_operator=True
        )
    with Session(engine) as s:
        got = vendor_dispatch.collect_outbound_queries(s, ids, "es-ES")
    refs = {q["ref"] for q in got[ids[0]]["queries"]}
    assert refs == {str(t_a), str(t_b)}, refs
    assert ids[1] not in got and ids[2] not in got, "no threads on the other segments"

    # Locale filter: a thread whose locale differs from the leg's does NOT ship.
    with Session(engine) as s:
        # forge a wrong-locale thread on seg1 by direct write (resolve_scope stamps es-ES).
        wrong = QueryThread(
            project_id=env["project_id"],
            document_id=env["doc_id"],
            segment_id=ids[1],
            target_locale="fr-FR",
            subject="fr",
            status=QueryStatus.OPEN,
            author="x@x.test",
        )
        s.add(wrong)
        s.commit()
    with Session(engine) as s:
        got2 = vendor_dispatch.collect_outbound_queries(s, ids, "es-ES")
    assert ids[1] not in got2, "an other-locale thread must not ship"

    # NO author email anywhere in the SERIALIZED package (privacy invariant, on bytes).
    with Session(engine) as s:
        qbs = vendor_dispatch.collect_outbound_queries(s, ids, "es-ES")
        step = s.exec(
            select(WorkflowStep).where(WorkflowStep.step_type == StepType.EXTERNAL)
        ).first()
        contract = vendor_dispatch.build_contract(
            run=WorkflowRun(
                id=1, workflow_id=env["wf_id"], document_id=env["doc_id"], target_locale="es-ES"
            ),
            step=step,
            segments=[
                {"id": i, "source": src, "target": ""} for i, src in zip(ids, SOURCES, strict=False)
            ],
            source_locale="en-US",
            target_locale="es-ES",
            claim_token="ct",
            queries_by_segment=qbs,
        )
    blob = json.dumps(contract, ensure_ascii=False)
    for email in ("ana@buyer.test", "ben@buyer.test", "cara@buyer.test"):
        assert email not in blob, ("an author email leaked into the package", email)
    assert '"author_role": "buyer"' in blob, "coarse author_role must ride instead"
    print("  [35] outbound carry: open ships, closed + other-locale excluded, no email in bytes OK")


def test_outbound_live_threads_survive_many_closed(_probe=False):
    """Gate H1: live threads must be SELECTED by the query, not filtered in Python off a
    50-row page. 2 live questions behind 55 CLOSED ones (closed sort NEWER on updated_at) must
    still ship -- the pre-fix code paged 50 closed and shipped ZERO. Revert-probe target."""
    env = _seed()
    ids = _seg_ids(env["doc_id"])
    live_a = _open_thread(segment_id=ids[0], subject="live-a", body="still open A")
    live_b = _open_thread(segment_id=ids[0], subject="live-b", body="still open B")
    # 55 threads opened AND closed AFTER the live ones -> newer updated_at, so they occupy the
    # first 50 rows of any updated_at-DESC page.
    for k in range(55):
        cid = _open_thread(segment_id=ids[0], subject=f"closed{k}", body="done")
        with Session(engine) as s:
            query_threads.set_status(
                s, user=None, thread_id=cid, status=QueryStatus.CLOSED, trusted_operator=True
            )
    with Session(engine) as s:
        got = vendor_dispatch.collect_outbound_queries(s, ids, "es-ES")
    refs = {q["ref"] for q in (got.get(ids[0], {}).get("queries") or [])}
    assert refs == {str(live_a), str(live_b)}, (
        "the 2 live threads must ship despite 55 newer closed ones (H1)",
        refs,
    )
    if not _probe:
        print(
            "  [35/H1] 2 live threads survive 55 newer closed ones (SQL-selected, not "
            "page-filtered) OK"
        )


def test_outbound_truncation_counts():
    env = _seed()
    ids = _seg_ids(env["doc_id"])
    # 7 open threads on seg0 -> ship 5, omit 2 (each has 1 post) -> queries_omitted == 2.
    for k in range(7):
        _open_thread(segment_id=ids[0], subject=f"q{k}", body=f"body {k}")
    with Session(engine) as s:
        got = vendor_dispatch.collect_outbound_queries(s, ids, "es-ES")
    blk = got[ids[0]]
    assert len(blk["queries"]) == vendor_dispatch._QUERIES_PER_SEGMENT
    assert blk["queries_truncated"] is True and blk["queries_omitted"] == 2, blk

    # 12 posts on one thread -> ship 10 newest, omit 2.
    env2 = _seed()
    ids2 = _seg_ids(env2["doc_id"])
    tid = _open_thread(segment_id=ids2[0], subject="chatty", body="post 0")
    with Session(engine) as s:
        for k in range(1, 12):
            query_threads.add_post(
                s, user=None, thread_id=tid, body=f"post {k}", trusted_operator=True
            )
    with Session(engine) as s:
        got2 = vendor_dispatch.collect_outbound_queries(s, ids2, "es-ES")
    blk2 = got2[ids2[0]]
    assert len(blk2["queries"][0]["posts"]) == vendor_dispatch._POSTS_PER_QUERY
    assert blk2["queries_truncated"] is True and blk2["queries_omitted"] == 2, blk2
    print("  [35] outbound truncation: 5-thread + 10-post caps declared with exact counts OK")


# ============================================================================
# [36] RETURN admission
# ============================================================================
def test_return_admits_reply_and_new_thread():
    env = _seed(assignee="rev@staff.test")
    _run, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    # A buyer thread on seg0 to reply to.
    tid = _open_thread(segment_id=ids[0], subject="Q", body="why literal?")
    comments = {
        ids[0]: [{"body": "kept the tag", "author_label": "Ana", "ref": "v-1", "reply_to": tid}],
        ids[1]: [
            {"body": "new note from vendor", "author_label": "Ben", "ref": "v-2", "reply_to": None}
        ],
    }
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s,
            _payload(
                ids, ["Hola.", "El gato.", "Tienes correo."], claim=claim, comments_by_id=comments
            ),
            resume="none",
            actor="tester",
        )
    assert out["comments_admitted"] == 2 and not out["comments_rejected"], out
    with Session(engine) as s:
        replied = s.get(QueryThread, tid)
        assert replied.status == QueryStatus.ANSWERED, "a vendor reply IS the answer"
        rposts = s.exec(select(QueryPost).where(QueryPost.thread_id == tid)).all()
        assert any(p.author_kind == "vendor" and p.body == "kept the tag" for p in rposts)
        # a new thread on seg1, assigned to the paused step's assignee.
        newt = s.exec(select(QueryThread).where(QueryThread.segment_id == ids[1])).first()
        assert newt is not None and newt.author_kind == "vendor"
        assert newt.assignee_email == "rev@staff.test", newt.assignee_email
    print("  [36] return admission: reply flips ANSWERED, no-reply opens an assigned thread OK")


def test_vendor_assignee_validated():
    # Issue #51 (vendor path — the THIRD assignee write site): the vendor writer now runs the
    # workflow-step assignee through the SAME _validate_assignee semantics in a SYSTEM context
    # (existence/active/customer-ring/project-visibility). A step naming a non-visible or
    # nonexistent address must NOT stamp an assignee (the silent black hole) and must NOT mail
    # it (relay-to-a-wrong-but-real user) — the vendor return still ADMITS. A real, project-
    # visible member is assigned + notified as before. Proven under auth ON + project_scoping
    # ON so the visibility gate is LIVE (auth OFF makes every user see-all, a no-op gate).
    from app.services import mailer, settings

    SENT = []
    orig_send = mailer.send
    mailer.send = lambda to, subj, body: (SENT.append(to), True)[1]
    settings.set_many(
        {
            "security_auth_required": "1",
            "project_scoping": "1",
            "notify_on_query": "1",
            "queries_enabled": "1",
        }
    )
    settings.invalidate_cache()
    try:
        # (a) a REAL active user with NO membership on the customer-bound project -> NOT visible.
        with Session(engine) as s:
            s.add(User(email="outsider@staff.test", name="Out", role=Role.REVIEWER, active=True))
            s.commit()
        env = _seed(assignee="outsider@staff.test", assignee_is_real=False)  # exists, no membership
        _run, _leg, claim = _dispatch(env)
        SENT.clear()  # drop the DISPATCH-time review-pause mail; isolate the vendor-RETURN path
        ids = _seg_ids(env["doc_id"])
        comments = {
            ids[1]: [{"body": "vendor note", "author_label": "Ben", "ref": "v-x", "reply_to": None}]
        }
        with Session(engine) as s:
            out = vendor_return.admit_manual(
                s,
                _payload(
                    ids,
                    ["Hola.", "El gato.", "Tienes correo."],
                    claim=claim,
                    comments_by_id=comments,
                ),
                resume="none",
                actor="tester",
            )
        assert out["comments_admitted"] == 1 and not out["comments_rejected"], out
        with Session(engine) as s:
            newt = s.exec(select(QueryThread).where(QueryThread.segment_id == ids[1])).first()
            assert newt is not None and newt.author_kind == "vendor"
            assert (newt.assignee_email or "") == "", (
                "a non-visible step assignee must leave the vendor thread UNASSIGNED",
                newt.assignee_email,
            )
        assert "outsider@staff.test" not in SENT, (
            "the non-visible assignee must never be mailed (relay-to-wrong-user)",
            SENT,
        )

        # (b) a real, project-visible MEMBER -> assigned as today + notified.
        env2 = _seed(assignee="member@staff.test", assignee_is_real=True)
        _run2, _leg2, claim2 = _dispatch(env2)
        SENT.clear()  # isolate the vendor-RETURN mail from the dispatch-time pause mail
        ids2 = _seg_ids(env2["doc_id"])
        comments2 = {
            ids2[1]: [
                {"body": "vendor note 2", "author_label": "Ana", "ref": "v-y", "reply_to": None}
            ]
        }
        with Session(engine) as s:
            out2 = vendor_return.admit_manual(
                s,
                _payload(
                    ids2,
                    ["Hola.", "El gato.", "Tienes correo."],
                    claim=claim2,
                    comments_by_id=comments2,
                ),
                resume="none",
                actor="tester",
            )
        assert out2["comments_admitted"] == 1, out2
        with Session(engine) as s:
            newt2 = s.exec(select(QueryThread).where(QueryThread.segment_id == ids2[1])).first()
            assert newt2.assignee_email == "member@staff.test", newt2.assignee_email
        assert "member@staff.test" in SENT, ("a valid member assignee must be notified", SENT)
    finally:
        mailer.send = orig_send
        settings.set_many(
            {
                "security_auth_required": "0",
                "project_scoping": "0",
                "notify_on_query": "1",
                "queries_enabled": "1",
            }
        )
        settings.invalidate_cache()
    print(
        "  [38] vendor assignee validated: a non-visible step address leaves the thread "
        "UNASSIGNED + unmailed (return still admits); a visible member is assigned + notified OK"
    )


def test_over_cap_and_malformed_refuse_whole_return():
    env = _seed()
    _run, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    over = [{"body": f"c{k}", "ref": f"r{k}"} for k in range(11)]  # 11 > 10
    for bad, label in (
        (over, "over-cap"),
        ([{"body": ""}], "empty body"),
        ([{"body": "ok", "reply_to": "not-int"}], "bad reply_to type"),
        ("notalist", "non-list comments"),
    ):
        with Session(engine) as s:
            try:
                vendor_return.admit_manual(
                    s,
                    _payload(ids, ["a", "b", "c"], claim=claim, comments_by_id={ids[0]: bad}),
                    resume="none",
                    actor="t",
                )
                assert False, f"{label}: the whole return must be refused"
            except vendor_return.ReturnContractError:
                pass
        with Session(engine) as s:
            leg = s.get(VendorDispatch, leg_id)
            assert leg.status == VendorDispatchStatus.DISPATCHED, (
                f"{label}: the leg must be UNTOUCHED after a refused return"
            )
            assert vendor_return.returned_ids(leg) == []
    print("  [36] a malformed / over-cap comments block refuses the WHOLE return, leg untouched OK")


# ============================================================================
# [37] N4 transaction law
# ============================================================================
def test_record_vendor_post_raise_still_commits():
    env = _seed()
    _run, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    orig = query_threads.record_vendor_post

    def _boom(*a, **k):
        raise RuntimeError("forced admission failure")

    query_threads.record_vendor_post = _boom
    # L1: capture the WARN (the degrade-to-WARN behaviour is what test 37 asserts) rather than
    # silencing it. Raise the propagated level to CRITICAL so the traceback is not printed, but
    # attach a catcher to the logger so we can assert the WARN record fired.
    _vlog = logging.getLogger("loc_tms.vendor_return")
    _catch = _WarnCatcher()
    _prev = _vlog.level
    _vlog.addHandler(_catch)
    _vlog.setLevel(logging.WARNING)
    _root_prev = logging.getLogger().level
    logging.getLogger().setLevel(logging.CRITICAL)  # keep the console quiet
    try:
        with Session(engine) as s:
            out = vendor_return.admit_manual(
                s,
                _payload(
                    ids,
                    ["Uno.", "Dos.", "Tres."],
                    claim=claim,
                    comments_by_id={ids[0]: [{"body": "x", "ref": "r1"}]},
                ),
                resume="none",
                actor="t",
            )
    finally:
        query_threads.record_vendor_post = orig
        _vlog.removeHandler(_catch)
        _vlog.setLevel(_prev)
        logging.getLogger().setLevel(_root_prev)
    # The OUTER commit succeeded: the leg RETURNED and the segments were written.
    assert out["complete"] is True, out
    assert out["comments_admitted"] == 0, "the failed comment recorded nothing"
    assert any(r.levelno >= logging.WARNING for r in _catch.records), (
        "the degraded comment must log a WARN (N4), not fail silently"
    )
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        assert leg.status == VendorDispatchStatus.RETURNED
        segs = orchestrator._load_segments(s, env["doc_id"], "es-ES")
        assert (segs[0].target_text or "") == "Uno.", "the vendor text still landed"
    print(
        "  [37] N4: record_vendor_post raising leaves the outer COMMIT intact (text "
        "written, comment dropped-with-WARN) OK"
    )


# ============================================================================
# [37b] Placement — a comment records regardless of the text outcome
# ============================================================================
def test_comment_records_before_every_content_branch():
    env = _seed()
    _run, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    # THREE distinct not-written branches, each reached by its own `continue` in _admit_units:
    #   seg0: HARD-LOCK      -> lock_refused (branch at :807)
    #   seg1: source-echo    -> no_op        (branch before the write)
    #   seg2: content REFUSAL -> flagged, `not outcome.committed` (branch at :818, L2)
    with Session(engine) as s:
        segs = orchestrator._load_segments(s, env["doc_id"], "es-ES")
        segs[0].lock_band = "exact"
        segs[0].lock_hard = True
        segs[0].status = SegStatus.APPROVED
        s.add(segs[0])
        s.commit()
    comments = {
        ids[0]: [{"body": "why locked?", "ref": "l1"}],
        ids[1]: [{"body": "echoed on purpose", "ref": "l2"}],
        ids[2]: [{"body": "I could not translate this", "ref": "l3"}],
    }
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s,
            _payload(
                ids,
                ["overwrite-attempt", SOURCES[1], "I cannot translate this content."],
                claim=claim,
                comments_by_id=comments,
            ),
            resume="none",
            actor="t",
        )
    assert str(ids[0]) in out["lock_refused"], out
    assert str(ids[1]) in out["no_op"], out
    assert str(ids[2]) in out["needs_review"], (
        "the refusal must reach the not-committed branch (flagged)",
        out,
    )
    assert out["comments_admitted"] == 3, (
        "all three comments recorded despite the text outcomes",
        out,
    )
    with Session(engine) as s:
        for sid, body in (
            (ids[0], "why locked?"),
            (ids[1], "echoed on purpose"),
            (ids[2], "I could not translate this"),
        ):
            t = s.exec(select(QueryThread).where(QueryThread.segment_id == sid)).first()
            assert t is not None, ("comment thread missing for", sid)
            assert any(
                p.body == body
                for p in s.exec(select(QueryPost).where(QueryPost.thread_id == t.id)).all()
            )
        # the post-edit capture did NOT fire for ANY non-committed segment.
        for sid in (ids[0], ids[1], ids[2]):
            assert not s.exec(
                select(SegmentPostEdit).where(SegmentPostEdit.segment_id == sid)
            ).all(), ("post-edit capture must not fire for a non-committed segment", sid)
    print(
        "  [37b] placement: comments record on hard-locked + echo + refusal-withheld "
        "segments; no post-edit capture for any OK"
    )


# ============================================================================
# [38] Idempotency (both branches)
# ============================================================================
def test_idempotent_reupload_no_dup_posts_or_threads():
    env = _seed()
    _run, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    tid = _open_thread(segment_id=ids[0], subject="Q", body="ask")
    comments = {
        ids[0]: [{"body": "reply", "ref": "v-1", "reply_to": tid}],
        ids[1]: [{"body": "fresh", "ref": "v-2"}],
    }
    p = _payload(ids, ["A.", "B.", "C."], claim=claim, comments_by_id=comments)
    with Session(engine) as s:
        vendor_return.admit_manual(s, p, resume="none", actor="t")
    # A second identical admit is refused at the id ledger (every id already returned), so
    # re-drive the vendor writer directly to prove the DEDUP, not the ledger gate.
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        before_posts = len(s.exec(select(QueryPost)).all())
        before_threads = len(s.exec(select(QueryThread)).all())
        sp = s.begin_nested()
        query_threads.record_vendor_post(
            s,
            leg=leg,
            segment_id=ids[0],
            body="reply",
            author_label="Ana",
            external_id="v-1",
            reply_to=tid,
        )
        query_threads.record_vendor_post(
            s,
            leg=leg,
            segment_id=ids[1],
            body="fresh",
            author_label="Ben",
            external_id="v-2",
            reply_to=None,
        )
        sp.commit()
        s.commit()
        after_posts = len(s.exec(select(QueryPost)).all())
        after_threads = len(s.exec(select(QueryThread)).all())
    assert after_posts == before_posts, (
        "a re-admitted comment duplicated a POST",
        before_posts,
        after_posts,
    )
    assert after_threads == before_threads, (
        "a re-admitted no-reply comment minted a second THREAD",
        before_threads,
        after_threads,
    )
    print("  [38] idempotency: re-admit dedups both the post AND the thread-creating branch OK")


# ============================================================================
# [38b] Cap breach -> comments_rejected, the return still admits
# ============================================================================
def test_cap_breach_and_invalid_reply_to_are_reported_not_fatal():
    env = _seed()
    _run, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    # A thread at its post cap so a reply breaches it -> QueryConflict -> comments_rejected.
    tid = _open_thread(segment_id=ids[0], subject="full", body="p0")
    orig_cap = query_threads.POSTS_PER_THREAD_MAX
    query_threads.POSTS_PER_THREAD_MAX = 1  # the opening post already fills it
    try:
        comments = {
            ids[0]: [
                {"body": "over the cap", "ref": "r-cap", "reply_to": tid},
                {"body": "bad target", "ref": "r-bad", "reply_to": 999999},
            ],
            ids[1]: [{"body": "this one is fine", "ref": "r-ok"}],
        }
        with Session(engine) as s:
            out = vendor_return.admit_manual(
                s,
                _payload(ids, ["A.", "B.", "C."], claim=claim, comments_by_id=comments),
                resume="none",
                actor="t",
            )
    finally:
        query_threads.POSTS_PER_THREAD_MAX = orig_cap
    assert out["complete"] is True, "the return still admits"
    rejected = out["comments_rejected"]
    refs = {r["ref"] for r in rejected}
    assert refs == {"r-cap", "r-bad"}, rejected
    for r in rejected:
        assert "segment_id" in r and r["reason"], r
    assert out["comments_admitted"] == 1, "the fine comment still landed"
    print(
        "  [38b] post-cap breach + invalid reply_to -> comments_rejected (seg id + ref + "
        "reason); return admits OK"
    )


def test_thread_creating_branch_honours_per_segment_cap():
    """Gate H3: a vendor NO-reply_to comment that would open the (MAX+1)th open thread on a
    segment is rejected individually (QueryConflict -> comments_rejected), NOT admitted as
    unbounded thread-minting; the return still admits. This is §11-38b's named thread-cap case
    (the real cap, not a monkeypatched post cap)."""
    env = _seed()
    _run, _leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    cap = query_threads.OPEN_THREADS_PER_SEGMENT_MAX
    # Fill the segment to its open-thread cap with human threads.
    for k in range(cap):
        _open_thread(segment_id=ids[0], subject=f"t{k}", body="q")
    with Session(engine) as s:
        out = vendor_return.admit_manual(
            s,
            _payload(
                ids,
                ["A.", "B.", "C."],
                claim=claim,
                comments_by_id={ids[0]: [{"body": "one too many", "ref": "r-x"}]},
            ),
            resume="none",
            actor="t",
        )
    assert out["complete"] is True, "the return still admits"
    assert out["comments_admitted"] == 0, "the over-cap thread must NOT be created"
    refs = {r["ref"] for r in out["comments_rejected"]}
    assert refs == {"r-x"}, out["comments_rejected"]
    assert "open-query limit" in out["comments_rejected"][0]["reason"], out["comments_rejected"]
    with Session(engine) as s:
        n = s.exec(
            select(func.count()).select_from(QueryThread).where(QueryThread.segment_id == ids[0])
        ).one()
        assert n == cap, ("no new thread minted past the cap", n, cap)
    print(
        "  [38b/H3] the thread-CREATING branch honours the per-segment open-thread cap "
        "(rejected, not unbounded minting) OK"
    )


# ============================================================================
# [38c] Kill switch on the wire
# ============================================================================
def test_kill_switch_omits_outbound_and_names_the_skip():
    env = _seed()
    ids = _seg_ids(env["doc_id"])
    _open_thread(segment_id=ids[0], subject="Q", body="ask")
    orig_d = vendor_dispatch._queries_enabled
    orig_r = vendor_return._queries_enabled
    set_pieces("app.services.vendor_dispatch", "_queries_enabled", lambda: False)
    set_pieces("app.services.vendor_return", "_queries_enabled", lambda: False)
    _SENT.clear()
    try:
        _run, _leg_id, claim = _dispatch(env)
        # L3: the ACTUAL wire bytes put on the (mock) transport carry NO `queries` key at all
        # (not an empty list) -- asserted on the real dispatched body, not a rebuilt dict.
        assert _SENT, "the dispatch must have put a body on the wire"
        assert all(b'"queries"' not in body for body in _SENT), (
            "queries must be OMITTED from the wire bytes when the feature is off"
        )
        # A return carrying comments admits, comments skipped -- but the skip is NAMED (M3/E5).
        with Session(engine) as s:
            out = vendor_return.admit_manual(
                s,
                _payload(
                    ids,
                    ["A.", "B.", "C."],
                    claim=claim,
                    comments_by_id={ids[0]: [{"body": "hi", "ref": "r"}]},
                ),
                resume="none",
                actor="t",
            )
        assert out["complete"] is True and out["comments_admitted"] == 0, out
        assert out["comments_skipped"] == "queries feature disabled", (
            "the skip must be DISCLOSED on the return report, not silent (E5)",
            out,
        )
        with Session(engine) as s:
            assert not s.exec(select(QueryPost).where(QueryPost.external_id == "r")).all(), (
                "no vendor post admitted when off"
            )
    finally:
        set_pieces("app.services.vendor_dispatch", "_queries_enabled", orig_d)
        set_pieces("app.services.vendor_return", "_queries_enabled", orig_r)
    print(
        "  [38c] kill switch: outbound omits the key on the WIRE; inbound skipped but the "
        "return report NAMES the skip (E5) OK"
    )


# ============================================================================
# [39] reply_to scoping — non-oracle
# ============================================================================
def test_reply_to_cross_scope_is_refused_as_non_oracle():
    # Two customers; a reply_to naming customer B's thread from customer A's leg is refused.
    envA = _seed()
    _runA, legA, claimA = _dispatch(envA)
    idsA = _seg_ids(envA["doc_id"])
    envB = _seed()
    idsB = _seg_ids(envB["doc_id"])
    foreign = _open_thread(segment_id=idsB[0], subject="B", body="foreign")
    nonexist = 10_000_000
    for bad_ref, label in ((foreign, "cross-customer"), (nonexist, "nonexistent")):
        with Session(engine) as s:
            out = vendor_return.admit_manual(
                s,
                _payload(
                    idsA,
                    ["A.", "B.", "C."],
                    claim=claimA,
                    comments_by_id={idsA[0]: [{"body": "x", "ref": "r", "reply_to": bad_ref}]},
                ),
                resume="none",
                actor="t",
            )
        # Both land in comments_rejected with the SAME shape -> indistinguishable (no oracle).
        assert out["complete"] is True
        assert len(out["comments_rejected"]) == 1, (label, out)
        # the foreign thread was NOT touched.
        with Session(engine) as s:
            ft = s.get(QueryThread, foreign)
            assert ft.status == QueryStatus.OPEN and ft.post_count == 1, (
                "a cross-tenant reply_to must never touch the foreign thread"
            )
        # reset the leg for the next iteration by re-dispatching a fresh one.
        _runA, legA, claimA = _dispatch(_seed())
        idsA = _seg_ids_for_leg(legA)
    print(
        "  [39] reply_to cross-scope + nonexistent both -> comments_rejected, foreign "
        "thread untouched (non-oracle) OK"
    )


def _seg_ids_for_leg(leg_id):
    with Session(engine) as s:
        leg = s.get(VendorDispatch, leg_id)
        return [int(x) for x in vendor_return.dispatched_ids(leg)]


# ============================================================================
# [H2] rework re-dispatch must carry the conversation
# ============================================================================
def test_rework_redispatch_carries_queries():
    from app.services import vendor_rework

    env = _seed()
    _run, leg_id, claim = _dispatch(env)
    ids = _seg_ids(env["doc_id"])
    # Complete the return so the leg is RETURNED (a rework requires a delivered return).
    with Session(engine) as s:
        vendor_return.admit_manual(
            s, _payload(ids, ["A.", "B.", "C."], claim=claim), resume="none", actor="t"
        )
    # A buyer query the rework round MUST NOT drop (§7.1/H2).
    _open_thread(segment_id=ids[0], subject="Tag?", body="keep the tag on rework")
    _SENT.clear()
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        with Session(engine) as s:
            leg = s.get(VendorDispatch, leg_id)
            vendor_rework.reject_and_rework(s, leg, note="fix tone", actor="t")
    finally:
        vendor_dispatch.set_test_transport(None)
    assert _SENT, "the rework must dispatch a package"
    assert any(b'"queries"' in body and b"keep the tag on rework" in body for body in _SENT), (
        "the rework re-dispatch must carry the buyer query conversation (H2)"
    )
    print("  [H2] rework re-dispatch carries the buyer query conversation (not lost per round) OK")


# ============================================================================
# [40] XLIFF — one way, anti-planting intact
# ============================================================================
def test_xliff_outbound_notes_and_inbound_admits_nothing():
    env = _seed()
    ids = _seg_ids(env["doc_id"])
    _open_thread(segment_id=ids[0], subject="Tag?", body="keep the tag")
    with Session(engine) as s:
        qbs = vendor_dispatch.collect_outbound_queries(s, ids, "es-ES")
    contract = {
        "source_locale": "en-US",
        "target_locale": "es-ES",
        "claim_token": "ct",
        "segments": [
            {"id": i, "source": src, "target": "", **(qbs.get(i) or {})}
            for i, src in zip(ids, SOURCES, strict=False)
        ],
    }
    xml = vendor_xliff.render_contract_xliff(contract).decode("utf-8")
    assert 'from="loc-studio-query"' in xml, "an outbound per-unit query note must render"
    assert "keep the tag" in xml

    # A return XLIFF carrying <note> elements admits NOTHING (only {id, target}).
    ret = (
        '<?xml version="1.0"?><xliff version="1.2" '
        'xmlns="urn:oasis:names:tc:xliff:document:1.2"><file target-language="es-ES">'
        '<body><trans-unit id="1"><source>Hello world.</source>'
        "<target>Hola mundo.</target>"
        '<note from="loc-studio-query">a planted reply</note>'
        '<note from="loc-studio-claim">planted-claim</note></trans-unit>'
        "</body></file></xliff>"
    )
    parsed = vendor_xliff.parse_return_xliff(ret)
    assert parsed["segments"] == [{"id": "1", "target": "Hola mundo."}], parsed
    assert "claim_token" not in parsed, "a trans-unit claim note must be ignored (anti-planting)"
    print(
        "  [40] XLIFF: outbound per-unit query note renders; inbound admits nothing; "
        "trans-unit claim note ignored OK"
    )


if __name__ == "__main__":
    test_outbound_carry_filters_and_declares_truncation()
    test_outbound_live_threads_survive_many_closed()
    test_outbound_truncation_counts()
    test_return_admits_reply_and_new_thread()
    test_vendor_assignee_validated()
    test_over_cap_and_malformed_refuse_whole_return()
    test_record_vendor_post_raise_still_commits()
    test_comment_records_before_every_content_branch()
    test_idempotent_reupload_no_dup_posts_or_threads()
    test_cap_breach_and_invalid_reply_to_are_reported_not_fatal()
    test_thread_creating_branch_honours_per_segment_cap()
    test_kill_switch_omits_outbound_and_names_the_skip()
    test_reply_to_cross_scope_is_refused_as_non_oracle()
    test_rework_redispatch_carries_queries()
    test_xliff_outbound_notes_and_inbound_admits_nothing()
    print("verify_vendor_queries: all OK")
