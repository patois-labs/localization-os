"""Fan-out batch dispatch verification (SPEC-workflows-redesign Phase 5a).

Hermetic + offline: throwaway LOC_TMS_DATA_DIR + provider keys blanked, so every
member run resolves through MockProvider (or an injected instrumenting provider) and
ZERO live calls are made. Covers the whole 5a surface:

  entity + migration        workflowbatch table + workflowrun.batch_id column exist; a
                            WorkflowBatch round-trips its JSON requested_locales; a plain
                            run has batch_id NULL (legacy byte-identity).
  dispatch happy path       3 locales -> 3 member runs, the CORRECT group variant resolved
                            per locale, all COMPLETED, batch status completed, notes None.
  bounded concurrency       max_parallel=1 serializes (max in-flight == 1, deterministic);
                            max_parallel=3 truly overlaps (a Barrier(3) rendezvous proves
                            3 members ran at once), both via an instrumenting provider.
  spend-stop                would_block trips after the first member -> members 2-3 never
                            launch, batch PARTIAL + notes name the spend stop + un-launched.
  fail-fast                 member 1 fails -> stop launching (2-3 unlaunched, PARTIAL);
                            WITHOUT fail_fast all 3 launch (mix -> PARTIAL).
  already-running skip      an active run for the same (workflow, doc, locale) NOT in the
                            batch -> that locale is SKIPPED (never adopted, never double-run).
  locales validation        a bogus locale is refused; an empty explicit list is refused;
                            None fans out to ALL the document's targets.
  CLI sync path             `workflow run --all-locales` completes in-process with every
                            member terminal + no leaked dispatcher/member threads.
  /api shapes               POST /api/workflows/{id}/run-batch + GET /api/batches/{id} return
                            the members[]+counts shape (5b's poll shape).
  history integration       a batch is ONE row (type=batch, link=/batches/{id}); member runs
                            are folded out; a plain run still shows as its own run row.
  status authority          finished_at is the terminal authority (RUNNING until stamped).

Final line: "BATCH RUNS OK".
"""

import io
import json as _json
import os
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_batch_")

from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Project,
    RunStatus,
    Segment,
    TargetLocale,
    TransMode,
    WorkflowBatch,
    WorkflowRun,
)
from app.services import apikeys, batch_runs, security, settings
from app.services import spend as spend_svc
from app.services import workflows as W
from app.services.providers import Provider, register_provider

init_db()
settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"})
settings.invalidate_cache()

_N = 0


def _uniq(prefix):
    global _N
    _N += 1
    return f"{prefix}-{_N}"


# --- instrumenting providers --------------------------------------------------
class CountingProvider(Provider):
    """Records concurrency: every complete() bumps a shared in-flight counter and the
    high-water mark. With a Barrier it forces a rendezvous (proves N ran at once);
    without one it sleeps briefly so a serialized run reads max-in-flight == 1."""

    name = "countmock"
    _lock = threading.Lock()
    inflight = 0
    max_inflight = 0
    calls = 0
    barrier = None

    @classmethod
    def reset(cls, barrier=None):
        with cls._lock:
            cls.inflight = 0
            cls.max_inflight = 0
            cls.calls = 0
        cls.barrier = barrier

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        cls = CountingProvider
        with cls._lock:
            cls.inflight += 1
            cls.max_inflight = max(cls.max_inflight, cls.inflight)
            cls.calls += 1
        try:
            if cls.barrier is not None:
                try:  # noqa: SIM105
                    cls.barrier.wait(timeout=8)
                except threading.BrokenBarrierError:
                    pass
            else:
                time.sleep(0.05)
            return "ok-" + ((payload or {}).get("text", "") or "x")
        finally:
            with cls._lock:
                cls.inflight -= 1


class FailPoisonProvider(Provider):
    """Raises for any segment whose source text is the poison marker (all attempts),
    succeeds otherwise — used to fail exactly ONE locale's member deterministically."""

    name = "failpoison"
    POISON = "FAILME"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        text = (payload or {}).get("text", "") or ""
        if self.POISON in text or self.POISON in (prompt or ""):
            raise RuntimeError("poisoned segment -> member must FAIL")
        return "ok-" + text


class BlockingProvider(Provider):
    """Holds every member in-flight: complete() blocks until the test SETS `release`.
    Used by the concurrent-batch double-run probe so a member stays genuinely ACTIVE
    (RUNNING) across the whole concurrent-dispatch window — the state the already-
    running skip must observe. Without this, a fast MockProvider member would COMPLETE
    before the second dispatcher's check, turning a would-be concurrent double-run into
    a benign sequential re-run and masking the race."""

    name = "blockmock"
    release = threading.Event()

    @classmethod
    def reset(cls):
        cls.release = threading.Event()

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        BlockingProvider.release.wait(timeout=15)
        return "ok-" + ((payload or {}).get("text", "") or "x")


register_provider("countmock", CountingProvider())
register_provider("failpoison", FailPoisonProvider())
register_provider("blockmock", BlockingProvider())


# --- fixtures -----------------------------------------------------------------
def _mk_project_doc(locales, source="en-US", poison=None):
    """A project (with TargetLocale rows) + a SEGMENTED doc carrying one segment per
    locale. The poison locale's segment source is the FailPoisonProvider marker."""
    with Session(engine) as s:
        p = Project(name=_uniq("BatchProj"), source_locale=source)
        s.add(p)
        s.commit()
        s.refresh(p)
        for loc in locales:
            s.add(TargetLocale(project_id=p.id, locale=loc))
        doc = Document(
            project_id=p.id,
            filename="batch.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Hello world."}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for loc in locales:
            src = FailPoisonProvider.POISON if loc == poison else "Hello world."
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=loc,
                    order_index=0,
                    source_text=src,
                    target_text="",
                )
            )
        s.commit()
        return p.id, doc.id


def _mk_wf(name, provider="mock", prompt="Translate {text}", src=None, tgt=None, retry_max=None):
    with Session(engine) as s:
        w = W.create_workflow(s, name)
        if src is not None or tgt is not None:
            w.source_locale, w.target_locale = src, tgt
            s.add(w)
            s.commit()
        st = W.add_step(
            s,
            w.id,
            name="Translator",
            provider=provider,
            model="m1",
            prompt_text=prompt,
            chunk_policy="none",
        )
        if retry_max is not None:
            st.retry_max = retry_max
            st.backoff_base = 0.0
            s.add(st)
            s.commit()
        return w.id


def _members(batch_id):
    with Session(engine) as s:
        return list(
            s.exec(
                select(WorkflowRun).where(WorkflowRun.batch_id == batch_id).order_by(WorkflowRun.id)
            ).all()
        )


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


# ============================================================================
# 1. entity + migration
# ============================================================================
def test_entity_and_migration():
    tables = set(inspect(engine).get_table_names())
    assert "workflowbatch" in tables, "workflowbatch table missing"
    run_cols = {c["name"] for c in inspect(engine).get_columns("workflowrun")}
    assert "batch_id" in run_cols, "workflowrun.batch_id column missing"
    with Session(engine) as s:
        b = WorkflowBatch(
            workflow_id=1,
            document_id=1,
            created_by="me",
            requested_locales=["es-ES", "fr-FR"],
            max_parallel=3,
            fail_fast=True,
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        got = s.get(WorkflowBatch, b.id)
        assert got.requested_locales == ["es-ES", "fr-FR"], got.requested_locales
        assert got.status == "running" and got.finished_at is None
        # a plain run has batch_id NULL (legacy byte-identity)
        r = WorkflowRun(workflow_id=1, document_id=1, status=RunStatus.COMPLETED)
        s.add(r)
        s.commit()
        s.refresh(r)
        assert r.batch_id is None, "a plain run must have a NULL batch_id"
    print(
        "  entity: workflowbatch + workflowrun.batch_id exist; JSON locales round-trip; "
        "plain run batch_id NULL OK"
    )


# ============================================================================
# 2. dispatch happy path — correct per-locale variant, all completed
# ============================================================================
def test_dispatch_happy_path_variants():
    locales = ["de-DE", "es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    # a group with one exact-pair variant per locale; the es-ES variant is the default
    # (the REQUESTED workflow) so resolve_variant must swap in fr/de per locale.
    with Session(engine) as s:
        grp = W.create_group(s, _uniq("HappyGrp"))
        gid = grp.id
    wf_es = _mk_wf("V es", src="en-US", tgt="es-ES")
    wf_fr = _mk_wf("V fr", src="en-US", tgt="fr-FR")
    wf_de = _mk_wf("V de", src="en-US", tgt="de-DE")
    with Session(engine) as s:
        W.add_to_group(
            s, wf_es, gid, source_locale="en-US", target_locale="es-ES", make_default=True
        )
        W.add_to_group(s, wf_fr, gid, source_locale="en-US", target_locale="fr-FR")
        W.add_to_group(s, wf_de, gid, source_locale="en-US", target_locale="de-DE")
        batch = batch_runs.start_batch(
            s, workflow_id=wf_es, doc_id=did, locales=None, created_by="tester", synchronous=True
        )
        bid = batch.id
        assert batch.status == "completed", (batch.status, batch.notes)
        assert batch.finished_at is not None
        assert sorted(batch.requested_locales) == locales
        assert batch.notes is None, batch.notes
    by_locale = {m.target_locale: m for m in _members(bid)}
    assert set(by_locale) == set(locales), by_locale
    assert by_locale["es-ES"].workflow_id == wf_es
    assert by_locale["fr-FR"].workflow_id == wf_fr, "fr-FR must resolve the fr variant"
    assert by_locale["de-DE"].workflow_id == wf_de, "de-DE must resolve the de variant"
    assert all(m.status == RunStatus.COMPLETED for m in by_locale.values())
    # every member carries the batch id; every member's segment got translated
    with Session(engine) as s:
        for m in by_locale.values():
            assert m.batch_id == bid
            seg = s.exec(
                select(Segment).where(
                    Segment.document_id == did, Segment.target_locale == m.target_locale
                )
            ).first()
            assert seg.target_text.strip(), f"{m.target_locale} segment not translated"
    print(
        "  happy path: 3 locales -> 3 members, correct variant per locale, all "
        "COMPLETED, batch completed, notes empty OK"
    )


# ============================================================================
# 3a. bounded concurrency — max_parallel=1 serializes (deterministic)
# ============================================================================
def test_max_parallel_1_serializes():
    locales = ["de-DE", "es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("SerialWf"), provider="countmock")
    CountingProvider.reset(barrier=None)  # sleep window, no rendezvous
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s,
            workflow_id=wf,
            doc_id=did,
            locales=None,
            created_by="t",
            max_parallel=1,
            synchronous=True,
        )
        assert batch.status == "completed", (batch.status, batch.notes)
    assert CountingProvider.calls == 3, ("one provider call per member", CountingProvider.calls)
    assert CountingProvider.max_inflight == 1, (
        "max_parallel=1 must serialize members",
        CountingProvider.max_inflight,
    )
    assert all(m.status == RunStatus.COMPLETED for m in _members(batch.id))
    print("  concurrency: max_parallel=1 -> max in-flight == 1 (serialized), all completed OK")


# ============================================================================
# 3b. bounded concurrency — max_parallel=3 truly overlaps (Barrier rendezvous)
# ============================================================================
def test_max_parallel_3_overlaps():
    locales = ["de-DE", "es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("ParallelWf"), provider="countmock")
    # A Barrier(3) can only release if all 3 members are in complete() AT ONCE — a
    # serialized dispatch would leave the first member waiting alone (timeout). So a
    # green rendezvous is a DETERMINISTIC proof of run-level concurrency.
    CountingProvider.reset(barrier=threading.Barrier(3))
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s,
            workflow_id=wf,
            doc_id=did,
            locales=None,
            created_by="t",
            max_parallel=3,
            synchronous=True,
        )
        assert batch.status == "completed", (batch.status, batch.notes)
    assert CountingProvider.calls == 3, CountingProvider.calls
    assert CountingProvider.max_inflight == 3, (
        "max_parallel=3 must let all 3 members overlap",
        CountingProvider.max_inflight,
    )
    print("  concurrency: max_parallel=3 -> Barrier(3) rendezvous (3 members overlapped) OK")


# ============================================================================
# 4. spend-stop — would_block trips after member 1 -> 2-3 unlaunched, PARTIAL
# ============================================================================
def test_spend_stop():
    locales = ["de-DE", "es-ES", "fr-FR"]  # sorted; de-DE launches first
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("SpendWf"), provider="mock")

    def _fake_would_block(now=None):
        # Block LAUNCHING once a member of THIS batch's document has COMPLETED
        # (deterministic under max_parallel=1: member 1 completes before member 2's gate
        # is evaluated). Scoped to `did` so COMPLETED members left by earlier tests in the
        # same process never pre-trip this one.
        with Session(engine) as s:
            done = s.exec(
                select(WorkflowRun.id).where(
                    WorkflowRun.document_id == did,
                    WorkflowRun.batch_id.isnot(None),
                    WorkflowRun.status == RunStatus.COMPLETED,
                )
            ).first()
        return (True, 12.0, 5.0) if done is not None else (False, 0.0, 0.0)

    orig = spend_svc.would_block
    spend_svc.would_block = _fake_would_block
    try:
        with Session(engine) as s:
            batch = batch_runs.start_batch(
                s,
                workflow_id=wf,
                doc_id=did,
                locales=None,
                created_by="t",
                max_parallel=1,
                synchronous=True,
            )
            bid = batch.id
            view = batch_runs.batch_as_dict(s, batch)
    finally:
        spend_svc.would_block = orig
    members = _members(bid)
    assert len(members) == 3, "every requested member must have a durable row"
    assert all(m.status == RunStatus.FAILED for m in members[1:])
    from app.services import orchestrator

    assert all(orchestrator.run_reason(m)["code"] == "spend_cap_stop" for m in members[1:])
    assert members[0].target_locale == "de-DE"
    assert members[0].status == RunStatus.COMPLETED
    assert view["status"] == "partial", view["status"]
    assert view["spend_stopped"] is True, view
    assert view["counts"]["unlaunched"] == 2 and set(view["unlaunched_locales"]) == {
        "es-ES",
        "fr-FR",
    }
    assert "Spend cap reached" in (view["notes"] or ""), view["notes"]
    print(
        "  spend-stop: would_block after member 1 -> members 2-3 unlaunched, PARTIAL + "
        "notes name the stop + the un-launched locales OK"
    )


# ============================================================================
# 5. fail-fast — member 1 fails stops launching; without it, all launch
# ============================================================================
def test_fail_fast():
    locales = ["de-DE", "es-ES", "fr-FR"]  # de-DE (poison) sorts first
    # --- WITH fail_fast: de-DE fails -> es/fr never launch ---
    _pid, did = _mk_project_doc(locales, poison="de-DE")
    wf = _mk_wf(_uniq("FailWf"), provider="failpoison", retry_max=0)
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s,
            workflow_id=wf,
            doc_id=did,
            locales=None,
            created_by="t",
            max_parallel=1,
            fail_fast=True,
            synchronous=True,
        )
        bid = batch.id
        view = batch_runs.batch_as_dict(s, batch)
    members = _members(bid)
    assert len(members) == 3 and members[0].target_locale == "de-DE"
    assert all(m.status == RunStatus.FAILED for m in members[1:])
    from app.services import orchestrator

    assert all(orchestrator.run_reason(m)["code"] == "fail_fast_stop" for m in members[1:])
    assert members[0].status == RunStatus.FAILED, members[0].status
    assert view["status"] == "partial", view["status"]
    assert view["counts"]["unlaunched"] == 2, view["counts"]
    assert "Fail-fast" in (view["notes"] or ""), view["notes"]

    # --- WITHOUT fail_fast: de-DE fails but es/fr still launch + complete ---
    _pid2, did2 = _mk_project_doc(locales, poison="de-DE")
    wf2 = _mk_wf(_uniq("NoFailWf"), provider="failpoison", retry_max=0)
    with Session(engine) as s:
        batch2 = batch_runs.start_batch(
            s,
            workflow_id=wf2,
            doc_id=did2,
            locales=None,
            created_by="t",
            max_parallel=1,
            fail_fast=False,
            synchronous=True,
        )
        view2 = batch_runs.batch_as_dict(s, batch2)
    m2 = {m.target_locale: m.status for m in _members(batch2.id)}
    assert set(m2) == set(locales), ("all 3 launch without fail_fast", m2)
    assert m2["de-DE"] == RunStatus.FAILED
    assert m2["es-ES"] == RunStatus.COMPLETED and m2["fr-FR"] == RunStatus.COMPLETED
    assert view2["status"] == "partial", view2["status"]  # a mix (1 failed, 2 done)
    print(
        "  fail-fast: member 1 failing stops launching (2 unlaunched); without it all 3 "
        "launch (mix -> partial) OK"
    )


# ============================================================================
# 6. already-running skip — never adopt a foreign run, never double-run
# ============================================================================
def test_already_running_skip():
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("SkipWf"), provider="mock")  # ungrouped -> resolves to itself
    # A pre-existing ACTIVE run for (wf, doc, fr-FR) that is NOT part of any batch.
    with Session(engine) as s:
        phantom = WorkflowRun(
            workflow_id=wf, document_id=did, target_locale="fr-FR", status=RunStatus.RUNNING
        )
        s.add(phantom)
        s.commit()
        s.refresh(phantom)
        phantom_id = phantom.id
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
        view = batch_runs.batch_as_dict(s, batch)
    members = _members(bid)
    assert len(members) == 1 and members[0].target_locale == "es-ES", (
        "fr-FR must be skipped (already running); es-ES launches",
        members,
    )
    assert view["status"] == "partial", view["status"]
    assert "already running" in (view["notes"] or "").lower(), view["notes"]
    assert view["counts"]["unlaunched"] == 1 and view["unlaunched_locales"] == ["fr-FR"]
    # the foreign run was NEVER adopted into the batch (its batch_id stays NULL)
    with Session(engine) as s:
        assert s.get(WorkflowRun, phantom_id).batch_id is None, "foreign run must not be adopted"
    print(
        "  already-running: same (wf,doc,locale) active elsewhere -> skipped + surfaced, "
        "foreign run not adopted OK"
    )


# ============================================================================
# 6b. GitHub #213 part 2 — the busy-locale skip is per (doc, locale), NOT per
#     workflow: a run of a DIFFERENT workflow on the locale is skipped + reported,
#     never launched as a rival member over one segment set.
# ============================================================================
def test_cross_workflow_already_running_skip():
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    rival_wf = _mk_wf(_uniq("RivalWf"), provider="mock")  # the workflow ALREADY running
    batch_wf = _mk_wf(_uniq("BatchWf"), provider="mock")  # the batch asks for a DIFFERENT one
    with Session(engine) as s:
        phantom = WorkflowRun(
            workflow_id=rival_wf, document_id=did, target_locale="fr-FR", status=RunStatus.RUNNING
        )
        s.add(phantom)
        s.commit()
        s.refresh(phantom)
        phantom_id = phantom.id
        batch = batch_runs.start_batch(
            s, workflow_id=batch_wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
        view = batch_runs.batch_as_dict(s, batch)
    members = _members(bid)
    assert [m.target_locale for m in members] == ["es-ES"], (
        "fr-FR carries a DIFFERENT workflow's run: skipped (already running), es-ES launches",
        members,
    )
    assert members[0].status == RunStatus.COMPLETED, members[0].status
    assert "already running" in (view["notes"] or "").lower(), view["notes"]
    assert view["counts"]["unlaunched"] == 1 and view["unlaunched_locales"] == ["fr-FR"]
    with Session(engine) as s:
        fresh = s.exec(
            select(WorkflowRun).where(
                WorkflowRun.document_id == did, WorkflowRun.target_locale == "fr-FR"
            )
        ).all()
        assert [x.id for x in fresh] == [phantom_id], (
            "the conflicting locale must NOT gain a second run row",
            [x.id for x in fresh],
        )
        assert s.get(WorkflowRun, phantom_id).batch_id is None
    print(
        "  #213 part 2: a DIFFERENT workflow's active run on a locale -> skipped + named "
        "in the notes, the other locale runs, no rival member created OK"
    )


# ============================================================================
# 6c. GitHub #213 fix round 1 — the busy-locale skip honours the SAME staleness
#     floor a single launch honours: a 7h-old RUNNING corpse of another workflow
#     (no batch, no live thread) that a single launch walks straight past must NOT
#     skip its locale in a batch. One segment set, one answer.
# ============================================================================
def test_stale_cross_workflow_corpse_does_not_skip_the_locale():
    from datetime import datetime, timedelta

    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    rival_wf = _mk_wf(_uniq("StaleRivalWf"), provider="mock")
    batch_wf = _mk_wf(_uniq("StaleBatchWf"), provider="mock")
    with Session(engine) as s:
        corpse = WorkflowRun(
            workflow_id=rival_wf,
            document_id=did,
            target_locale="fr-FR",
            status=RunStatus.RUNNING,
            started_at=datetime.utcnow() - timedelta(hours=7),
        )
        s.add(corpse)
        s.commit()
        corpse_id = corpse.id
        batch = batch_runs.start_batch(
            s, workflow_id=batch_wf, doc_id=did, locales=None, created_by="t", synchronous=True
        )
        bid = batch.id
        view = batch_runs.batch_as_dict(s, batch)
    members = _members(bid)
    assert sorted(m.target_locale for m in members) == ["es-ES", "fr-FR"], (
        "a 7h-old DIFFERENT-workflow corpse must not skip its locale: the staleness "
        f"floor a single launch honours applies to the batch skip too, members={members!r}"
    )
    assert all(m.status == RunStatus.COMPLETED for m in members), members
    assert view["status"] == "completed", view["status"]
    assert "already running" not in (view["notes"] or "").lower(), view["notes"]
    assert view["counts"]["unlaunched"] == 0, view["counts"]
    with Session(engine) as s:
        rival_rows = s.exec(
            select(WorkflowRun).where(
                WorkflowRun.document_id == did, WorkflowRun.target_locale == "fr-FR"
            )
        ).all()
        # exactly two rows on the locale: the batch member that ran + the untouched
        # corpse, which stays un-adopted (batch_id NULL, its own healer's business).
        member = next(x for x in rival_rows if x.batch_id == bid)
        assert member.status == RunStatus.COMPLETED, member.status
        assert s.get(WorkflowRun, corpse_id).batch_id is None, "the corpse was adopted"
    print(
        "  #213 fix 1: a STALE DIFFERENT-workflow corpse no longer skips its locale in "
        "a batch (same staleness floor as a single launch); the locale runs OK"
    )


# ============================================================================
# 7. locales validation
# ============================================================================
def test_locales_validation():
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("ValWf"), provider="mock")
    with Session(engine) as s:
        # a bogus locale (not a target of the doc) is refused
        try:
            batch_runs.start_batch(
                s,
                workflow_id=wf,
                doc_id=did,
                locales=["es-ES", "zz-ZZ"],
                created_by="t",
                synchronous=True,
            )
            raise AssertionError("a bogus locale must raise BatchError")
        except batch_runs.BatchError as exc:
            assert "zz-ZZ" in str(exc), str(exc)
        # an explicit EMPTY list is refused
        try:
            batch_runs.start_batch(
                s, workflow_id=wf, doc_id=did, locales=[], created_by="t", synchronous=True
            )
            raise AssertionError("an empty explicit locale list must raise")
        except batch_runs.BatchError:
            pass
        # a subset explicitly given is honored
        b_sub = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=["fr-FR"], created_by="t", synchronous=True
        )
        assert b_sub.requested_locales == ["fr-FR"]
        assert len(_members(b_sub.id)) == 1
    print(
        "  validation: bogus locale refused; empty list refused; explicit subset honored; "
        "None = all targets OK"
    )


# ============================================================================
# 8. CLI sync path — completes in-process, no daemon/thread leakage
# ============================================================================
def test_cli_sync_no_daemon_leak():
    for _ in range(12):
        _cli_sync_no_daemon_leak_once()
    print("  synchronous CLI thread joins: 12 of 12 OK")


def _cli_sync_no_daemon_leak_once():
    locales = ["es-ES", "fr-FR", "de-DE"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("CliWf"), provider="mock")
    before = {t.ident for t in threading.enumerate()}
    code, out, err = _run_cli(["--json", "workflow", "run", str(did), str(wf), "--all-locales"])
    assert code == 0, (code, out, err)
    payload = _json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is True and payload["status"] == "completed", payload
    assert payload["kind"] == "batch" and len(payload["members"]) == 3, payload
    bid = payload["id"]
    # EVERY member is terminal by the time the synchronous CLI returned (no fire-and-forget)
    members = _members(bid)
    assert len(members) == 3
    assert all(
        m.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.PAUSED) for m in members
    ), [m.status for m in members]
    assert all(m.status == RunStatus.COMPLETED for m in members)
    # no leaked batch dispatcher / member threads survive the call
    leaked = [
        t.name for t in threading.enumerate() if t.ident not in before and "loc-batch" in t.name
    ]
    assert not leaked, ("synchronous CLI must join every batch thread", leaked)
    print(
        "  CLI sync: --all-locales completes in-process, all members terminal, no leaked "
        "dispatcher/member threads OK"
    )


# ============================================================================
# 9. /api shapes — run-batch + batches/{id} (5b poll shape)
# ============================================================================
def test_api_shapes():
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("ApiWf"), provider="mock")
    admin = security.create_user("batch-admin@x.io", "batchadmin", "admin", "fixture-pass-1234!!")
    _row, raw = apikeys.create_key(admin.id, "batch-key", scopes="api docs")
    client = TestClient(app)
    h = {"X-Loc-Token": raw}
    # wait=true (AUDIT Q2): the DEFAULT run-batch now dispatches ASYNC (returns a RUNNING
    # batch immediately, so it never blocks the server event loop); the finalized-shape
    # assertions below use the opt-in wait path, which runs the SAME dispatch OFF the loop
    # in a threadpool worker and returns the finalized batch. (The async default is covered
    # in verify_api_workflow_run_async.)
    r = client.post(
        f"/api/workflows/{wf}/run-batch", headers=h, json={"doc_id": did, "wait": True}
    )  # no locales -> all
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["kind"] == "batch" and body["status"] == "completed", body
    assert body["counts"]["requested"] == 2 and body["counts"]["completed"] == 2, body
    mkeys = {"locale", "run_id", "status", "current_step_index", "resolved_workflow_id"}
    assert body["members"] and all(mkeys <= set(m) for m in body["members"]), body["members"]
    bid = body["id"]
    # GET mirror returns the same shape
    r2 = client.get(f"/api/batches/{bid}", headers=h)
    assert r2.status_code == 200, r2.text
    got = r2.json()
    assert got["ok"] is True and got["id"] == bid and got["status"] == "completed"
    assert {m["locale"] for m in got["members"]} == set(locales), got
    # a missing batch is a clean 404
    assert client.get("/api/batches/999999", headers=h).status_code == 404
    print("  /api: run-batch + batches/{id} return the members[]+counts shape; missing -> 404 OK")


# ============================================================================
# 10. web route + minimal page (the async daemon path)
# ============================================================================
def test_web_route_and_page():
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("WebWf"), provider="mock")
    client = TestClient(app)  # auth off -> no CSRF required
    r = client.post(
        f"/documents/{did}/run-batch", data={"workflow_id": str(wf)}, follow_redirects=False
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    loc = r.headers["location"]
    assert loc.startswith("/batches/"), loc
    bid = int(loc.rsplit("/", 1)[1])
    # the daemon dispatcher finishes asynchronously; poll the terminal authority
    for _ in range(80):
        with Session(engine) as s:
            b = s.get(WorkflowBatch, bid)
            if b.finished_at is not None:
                break
        time.sleep(0.1)
    with Session(engine) as s:
        b = s.get(WorkflowBatch, bid)
        assert b.finished_at is not None, "daemon dispatcher never finalized the batch"
        assert b.status == "completed", (b.status, b.notes)
    page = client.get(f"/batches/{bid}")
    assert page.status_code == 200, page.text[:200]
    assert "es-ES" in page.text and "fr-FR" in page.text, "member locales absent from page"
    print("  web: POST run-batch -> 303 /batches/{id}; daemon finalizes; page renders members OK")


# ============================================================================
# 11. history integration — one batch row, member runs folded out
# ============================================================================
def test_history_integration():
    from app.services import job_history

    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("HistWf"), provider="mock")
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="hist@x", synchronous=True
        )
        bid = batch.id
        member_ids = {m.id for m in _members(bid)}
        rows, _total = job_history.unified_history(s, None, limit=200)
    batch_rows = [r for r in rows if r.type == "batch" and r.id == bid]
    assert len(batch_rows) == 1, "exactly one batch row expected"
    br = batch_rows[0]
    assert br.link == f"/batches/{bid}" and br.kind and br.actor == "hist@x", br
    assert br.progress_total == 2 and br.progress_done == 2, (br.progress_done, br.progress_total)
    # member runs are folded OUT of the run rows (calm history)
    leaked = [r for r in rows if r.type == "run" and r.id in member_ids]
    assert not leaked, "batch member runs must not appear as separate run rows"
    # a plain (non-batch) run still shows as its own run row
    with Session(engine) as s:
        plain = WorkflowRun(
            workflow_id=wf,
            document_id=did,
            target_locale="es-ES",
            status=RunStatus.COMPLETED,
            created_by="plain@x",
        )
        s.add(plain)
        s.commit()
        s.refresh(plain)
        plain_id = plain.id
        assert plain.batch_id is None
        rows2, _ = job_history.unified_history(s, None, limit=200)
    assert any(r.type == "run" and r.id == plain_id for r in rows2), (
        "plain run missing from history"
    )
    # a type=batch filter returns only batches
    with Session(engine) as s:
        brows, _ = job_history.unified_history(s, None, type="batch", limit=200)
    assert brows and all(r.type == "batch" for r in brows), "type=batch must return only batches"
    print(
        "  history: one batch row (link /batches/{id}), members folded out, plain run intact, "
        "type=batch filter works OK"
    )


# ============================================================================
# 12. status authority — RUNNING until finished_at, then the stored rollup
# ============================================================================
def test_status_authority():
    with Session(engine) as s:
        b = WorkflowBatch(
            workflow_id=1, document_id=1, requested_locales=["es-ES"], status="completed"
        )  # stored terminal, but NOT finished
        s.add(b)
        s.commit()
        s.refresh(b)
        # finished_at is the authority: RUNNING is shown until it is stamped, even though
        # the stored column says 'completed' (guards against a stale terminal read).
        assert batch_runs.batch_status(s, b) == "running", "unfinished batch must read running"
        from datetime import datetime

        b.finished_at = datetime.utcnow()
        s.add(b)
        s.commit()
        assert batch_runs.batch_status(s, b) == "completed", "finished batch reads stored status"
    print("  status authority: finished_at gates the terminal read (running until stamped) OK")


# ============================================================================
# 13. concurrent batches must NOT double-run the same (workflow, doc, locale)
#     (adversarial-review HIGH — the check-and-create TOCTOU)
# ============================================================================
def test_concurrent_batches_no_double_run():
    """Two batches dispatched CONCURRENTLY over the SAME document must never each
    launch a member for the same (workflow, doc, locale): the check (_active_run_
    elsewhere) and the create (_create_member) must be atomic across dispatchers, or
    both read 'nothing active' and both launch — two runs racing writes to the same
    Segment rows. We force the check-collision deterministically on ONE locale with a
    timed handshake (safe with the fix: the second dispatcher is serialized on the lock
    and simply times out, never deadlocks) while a blocking provider holds every member
    genuinely RUNNING across the window."""
    from app.services import orchestrator  # noqa: F401

    locales = ["de-DE", "es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    # ungrouped workflow -> both batches resolve every locale to the SAME workflow id,
    # so a collision is a genuine same-(wf,doc,locale) double-run.
    wf = _mk_wf(_uniq("RaceWf"), provider="blockmock")
    BlockingProvider.reset()
    COORD = "de-DE"  # the locale we force a simultaneous check on
    arr = {"n": 0}
    arr_lock = threading.Lock()
    second = threading.Event()
    orig_check = batch_runs._active_run_elsewhere

    def coord_check(session, workflow_id, doc_id, locale, batch_id):
        # compute the real result FIRST, THEN (for the coordinated locale only) hold both
        # dispatchers at the post-check point so — absent the lock — BOTH would proceed to
        # create before either commits. With the fix the second dispatcher can't even reach
        # here until the first has created+committed+released the lock, so its real result
        # already reflects the first member and it SKIPS.
        result = orig_check(session, workflow_id, doc_id, locale, batch_id)
        if locale == COORD and doc_id == did:
            with arr_lock:
                arr["n"] += 1
                me = arr["n"]
            if me == 1:
                second.wait(timeout=0.6)  # first waits for the second (times out under lock)
            elif me == 2:
                second.set()
        return result

    ids = {}

    def run_batch(tag):
        with Session(engine) as s:
            b = batch_runs.start_batch(
                s,
                workflow_id=wf,
                doc_id=did,
                locales=list(locales),
                created_by=tag,
                max_parallel=3,
                synchronous=True,
            )
            ids[tag] = b.id

    set_pieces("app.services.batch_runs", "_active_run_elsewhere", coord_check)
    dispatchers = [
        threading.Thread(target=run_batch, args=(t,), name=f"race-{t}") for t in ("A", "B")
    ]
    try:
        for t in dispatchers:
            t.start()
        # Members block in the provider (release not set), so both dispatchers finish their
        # per-locale loops (create/skip) and then park at their member-join. Wait until the
        # member count stabilizes (every locale has its member), THEN release.
        deadline = time.time() + 6.0
        last = -1
        while time.time() < deadline:
            with Session(engine) as s:
                cnt = len(
                    s.exec(
                        select(WorkflowRun.id)
                        .where(WorkflowRun.document_id == did, WorkflowRun.batch_id.isnot(None))
                        .where(WorkflowRun.status == RunStatus.RUNNING)
                    ).all()
                )
            if cnt == last and cnt >= len(locales):
                break
            last = cnt
            time.sleep(0.15)
        BlockingProvider.release.set()
        for t in dispatchers:
            t.join(timeout=20)
    finally:
        BlockingProvider.release.set()  # never leave a member blocked
        set_pieces("app.services.batch_runs", "_active_run_elsewhere", orig_check)
    assert not any(t.is_alive() for t in dispatchers), "a dispatcher thread hung"
    # THE INVARIANT: across BOTH batches, every locale has exactly ONE member run — the
    # second batch saw the first's member and skipped, never double-running the segment set.
    with Session(engine) as s:
        for loc in locales:
            ms = s.exec(
                select(WorkflowRun).where(
                    WorkflowRun.document_id == did,
                    WorkflowRun.target_locale == loc,
                    WorkflowRun.batch_id.isnot(None),
                )
            ).all()
            assert len(ms) == 1, (
                f"concurrent batches double-ran {loc}: {len(ms)} member runs "
                f"(the check-and-create was not serialized)"
            )
    print(
        "  concurrency: two batches over one doc -> each locale exactly ONE member "
        "(check-and-create serialized; no concurrent double-run) OK"
    )


# ============================================================================
# 13b. two concurrent RE-RUNS of the SAME locale INTO ONE batch must not double-run
#      (adversarial-review MED — the create-guard excluded same-batch members, so the
#      cross-batch serialization did NOT cover a same-batch rerun race)
# ============================================================================
def test_concurrent_reruns_no_double_run():
    """A double-clicked 'Re-run' (or two overlapping retry-failed calls) for the SAME locale
    must never each launch a fresh member into the SAME batch. rerun_batch's pre-dispatch
    active check has a TOCTOU window before a dispatcher commits its new member; the real
    safety net is the create-guard (_active_run_elsewhere under _RUN_CREATE_LOCK). It must
    block a concurrently-created ACTIVE SAME-batch sibling exactly as it blocks a cross-batch
    one. Before the fix the guard EXCLUDED same-batch members, so two reruns that both passed
    the pre-dispatch check each created a second RUNNING member for the locale (2x provider
    spend + last-writer-wins on the segment set). Structured like test 13's cross-batch probe
    but re-dispatching into ONE batch: a timed handshake delays the first dispatcher's create
    so BOTH reruns clear the pre-dispatch guard, then the lock decides."""
    from app.services import orchestrator  # noqa: F401

    locales = ["de-DE", "es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    # ungrouped workflow -> every locale resolves to the SAME workflow id, so a collision is a
    # genuine same-(wf,doc,locale) double-run. blockmock lets us hold the RE-RUN members RUNNING.
    wf = _mk_wf(_uniq("RerunRaceWf"), provider="blockmock")
    # Initial batch completes FAST (release pre-set) so every locale has ONE terminal member.
    BlockingProvider.reset()
    BlockingProvider.release.set()
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wf, doc_id=did, locales=None, created_by="init", synchronous=True
        )
        bid = batch.id
        assert batch.status == "completed", (batch.status, batch.notes)
    before = {loc: len([m for m in _members(bid) if m.target_locale == loc]) for loc in locales}
    assert all(before[loc] == 1 for loc in locales), ("each locale one terminal member", before)
    # Now the RE-RUN members must stay genuinely RUNNING across the race window.
    BlockingProvider.reset()  # clears the event -> complete() blocks until released
    COORD = "de-DE"  # the locale we drive two concurrent reruns at
    arr = {"n": 0}
    arr_lock = threading.Lock()
    second = threading.Event()
    orig_check = batch_runs._active_run_elsewhere

    def coord_check(session, workflow_id, doc_id, locale, batch_id):
        # Compute the REAL result first, then (for the coordinated locale) hold the first
        # dispatcher at the post-check/pre-create point. The second dispatcher is serialized on
        # _RUN_CREATE_LOCK and can't reach here, so the first simply times out — the delay just
        # guarantees BOTH reruns cleared rerun_batch's pre-dispatch guard before either created.
        result = orig_check(session, workflow_id, doc_id, locale, batch_id)
        if locale == COORD and doc_id == did:
            with arr_lock:
                arr["n"] += 1
                me = arr["n"]
            if me == 1:
                second.wait(timeout=0.6)
            elif me == 2:
                second.set()
        return result

    errors: dict = {}

    def do_rerun(tag):
        try:
            with Session(engine) as s:
                b = s.get(WorkflowBatch, bid)
                batch_runs.rerun_batch(
                    s, batch=b, locales=[COORD], created_by=tag, synchronous=True
                )
        except batch_runs.BatchError as exc:
            # a pre-dispatch refusal ("still running") is ALSO a valid no-double-run outcome
            errors[tag] = str(exc)

    set_pieces("app.services.batch_runs", "_active_run_elsewhere", coord_check)
    reruns = [threading.Thread(target=do_rerun, args=(t,), name=f"rerun-{t}") for t in ("A", "B")]
    try:
        for t in reruns:
            t.start()
        # The rerun member(s) block in the provider; wait until COORD's RUNNING count stabilizes,
        # THEN release so the dispatchers can join.
        deadline = time.time() + 6.0
        last = -1
        while time.time() < deadline:
            with Session(engine) as s:
                cnt = len(
                    s.exec(
                        select(WorkflowRun.id).where(
                            WorkflowRun.document_id == did,
                            WorkflowRun.target_locale == COORD,
                            WorkflowRun.batch_id.isnot(None),
                            WorkflowRun.status == RunStatus.RUNNING,
                        )
                    ).all()
                )
            if cnt == last and cnt >= 1:
                break
            last = cnt
            time.sleep(0.15)
        BlockingProvider.release.set()
        for t in reruns:
            t.join(timeout=20)
    finally:
        BlockingProvider.release.set()  # never leave a member blocked
        set_pieces("app.services.batch_runs", "_active_run_elsewhere", orig_check)
    assert not any(t.is_alive() for t in reruns), "a rerun dispatcher hung"
    # THE INVARIANT: the two reruns added AT MOST ONE fresh member for COORD (total 2 = the
    # original attempt + a single re-run), never two concurrent new members.
    coord_members = [m for m in _members(bid) if m.target_locale == COORD]
    assert len(coord_members) == 2, (
        f"concurrent reruns double-ran {COORD}: {len(coord_members)} members "
        f"(the create-guard did not block a same-batch active sibling)"
    )
    assert all(m.status in (RunStatus.COMPLETED, RunStatus.FAILED) for m in coord_members), (
        "the re-run member must be terminal after release, not a leaked RUNNING",
        [m.status for m in coord_members],
    )
    # a COORD-only rerun must never touch the other locales
    for loc in ("es-ES", "fr-FR"):
        assert len([m for m in _members(bid) if m.target_locale == loc]) == 1, (
            f"{loc} must be untouched by a {COORD}-only rerun"
        )
    print(
        "  concurrency: two reruns of one locale into one batch -> exactly ONE fresh member "
        "(same-batch double-run blocked by the create-guard) OK"
    )


# ============================================================================
# 14. a manual single run must NOT be swallowed by an in-flight batch member of a
#     DIFFERENT locale (adversarial-review MED — locale-blind re-attach)
# ============================================================================
def test_single_run_not_swallowed_by_batch_member():
    """orchestrator.start_run's double-submit guard must match target_locale: while a
    batch member for fr-FR is RUNNING, start_run(wf, doc, 'it-IT') must launch a REAL
    it-IT run, not re-attach to the fr-FR member. And a SAME-locale single run must
    still re-attach to the active run (the genuine double-submit case — no double-run)."""
    from app.services import orchestrator

    locales = ["fr-FR", "it-IT"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("SwallowWf"), provider="mock")  # ungrouped -> resolves to itself
    # Simulate an in-flight batch member for fr-FR (RUNNING, batch_id set).
    with Session(engine) as s:
        b = WorkflowBatch(
            workflow_id=wf, document_id=did, requested_locales=locales, status="running"
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        member = WorkflowRun(
            workflow_id=wf,
            document_id=did,
            target_locale="fr-FR",
            status=RunStatus.RUNNING,
            batch_id=b.id,
        )
        s.add(member)
        s.commit()
        s.refresh(member)
        member_id = member.id
    # A manual single run for a DIFFERENT locale must NOT return the fr-FR member id.
    with Session(engine) as s:
        rid = orchestrator.start_run(s, wf, did, "it-IT", created_by="u")
    assert rid != member_id, (
        "it-IT single run re-attached to the fr-FR batch member (locale-blind guard)"
    )
    with Session(engine) as s:
        r = s.get(WorkflowRun, rid)
        assert r is not None and r.target_locale == "it-IT", (
            "the launched run must actually be for it-IT",
            getattr(r, "target_locale", None),
        )
    # let the background it-IT run reach terminal (avoid a leaked worker thread)
    for _ in range(100):
        with Session(engine) as s:
            r = s.get(WorkflowRun, rid)
            if r.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.PAUSED):
                break
        time.sleep(0.05)
    # A SAME-locale single run re-attaches to the still-active fr-FR member (no double-run).
    with Session(engine) as s:
        rid_same = orchestrator.start_run(s, wf, did, "fr-FR", created_by="u")
    assert rid_same == member_id, (
        "a same-locale single run must re-attach to the active member (no double-run)"
    )
    print(
        "  single-vs-batch: a single run for a different locale is not swallowed by a "
        "batch member; a same-locale run re-attaches (no double-run) OK"
    )


# ============================================================================
# 15. a mid-loop dispatch error must STILL finalize the batch (no RUNNING zombie)
#     (adversarial-review LOW — _dispatch had no try/finally)
# ============================================================================
def test_dispatch_finalizes_on_midloop_error():
    locales = ["de-DE", "es-ES", "fr-FR"]  # de-DE launches first, es-ES trips the error
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("FinalizeWf"), provider="mock")
    orig_create = batch_runs._create_member

    def boom(session, workflow_id, doc_id, locale, created_by, batch_id):
        if locale == "es-ES":
            raise RuntimeError("simulated mid-loop create failure")
        return orig_create(session, workflow_id, doc_id, locale, created_by, batch_id)

    set_pieces("app.services.batch_runs", "_create_member", boom)
    try:
        with Session(engine) as s:
            batch = batch_runs.start_batch(
                s,
                workflow_id=wf,
                doc_id=did,
                locales=None,
                created_by="t",
                max_parallel=1,
                synchronous=True,
            )
            bid = batch.id
            assert batch.finished_at is not None, (
                "a mid-loop create error must still finalize the batch (finished_at stamped)"
            )
            status = batch_runs.batch_status(s, batch)
    finally:
        set_pieces("app.services.batch_runs", "_create_member", orig_create)
    assert status != "running", ("a finalized batch must not read RUNNING", status)
    members = _members(bid)
    assert any(m.target_locale == "de-DE" for m in members), (
        "de-DE launched before the es-ES failure and must be a member",
        members,
    )
    print(
        "  finalize-guard: a mid-loop create error still finalizes the batch "
        "(finished_at stamped, not a RUNNING zombie) OK"
    )


# ============================================================================
# 16. recover_interrupted_batches heals a restart-orphaned batch (member + rollup)
#     (adversarial-review LOW — batch zombie was unhealed at startup)
# ============================================================================
def test_recover_interrupted_batches():
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("HealWf"), provider="mock")
    # A batch cut off mid-flight by a restart: finished_at NULL, members stuck RUNNING.
    with Session(engine) as s:
        b = WorkflowBatch(
            workflow_id=wf, document_id=did, requested_locales=locales, status="running"
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        bid = b.id
        for loc in locales:
            s.add(
                WorkflowRun(
                    workflow_id=wf,
                    document_id=did,
                    target_locale=loc,
                    status=RunStatus.RUNNING,
                    batch_id=bid,
                )
            )
        s.commit()
    touched = batch_runs.recover_interrupted_batches(at_boot=True)
    assert touched >= 1, ("the healer must touch at least the interrupted batch", touched)
    with Session(engine) as s:
        b = s.get(WorkflowBatch, bid)
        assert b.finished_at is not None, "interrupted batch must be finalized by the healer"
        assert batch_runs.batch_status(s, b) != "running", ("no forever-RUNNING zombie", b.status)
        assert "Interrupted" in (b.notes or ""), b.notes
        members = _members(bid)
        assert members and all(m.status == RunStatus.FAILED for m in members), (
            "orphaned RUNNING members must be failed",
            [m.status for m in members],
        )
        assert all(m.finished_at is not None for m in members), "healed members need finished_at"
    print(
        "  heal: recover_interrupted_batches fails orphaned members + finalizes the "
        "zombie batch (no forever-RUNNING) OK"
    )


# ============================================================================
# 17. the RUNNING batch page auto-reload is BOUNDED (adversarial-review LOW —
#     an unbounded 2.5s reload loop on a zombie batch)
# ============================================================================
def test_batch_page_reload_is_bounded():
    locales = ["es-ES", "fr-FR"]
    _pid, did = _mk_project_doc(locales)
    wf = _mk_wf(_uniq("ReloadWf"), provider="mock")
    # A batch that reads RUNNING (finished_at NULL) so the page renders its live poll.
    with Session(engine) as s:
        b = WorkflowBatch(
            workflow_id=wf, document_id=did, requested_locales=locales, status="running"
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        bid = b.id
    client = TestClient(app)
    page = client.get(f"/batches/{bid}")
    assert page.status_code == 200, page.text[:200]
    html = page.text
    assert "window.location.reload" in html, "the live reload script is missing"
    # The reload must be BOUNDED by a hard cap (a per-batch reload counter), not an
    # unconditional forever-reload — mirrors observe_run's max_seconds ceiling.
    assert "batch-reloads-" in html, "the bounded per-batch reload counter is missing"
    assert "BATCH_RELOAD_MAX" in html, "the reload cap constant is missing"
    print(
        "  reload-bound: a RUNNING batch page auto-reloads under a hard per-batch cap "
        "(no unbounded forever-reload loop) OK"
    )


def _mk_fulltext_project_doc(locales, source="en-US"):
    """A project + a FULL_TEXT doc (one whole-body segment per locale). Enough for the
    batch _validate guard, which only reads doc.mode + the workflow's step types."""
    with Session(engine) as s:
        p = Project(name=_uniq("BatchFTProj"), source_locale=source)
        s.add(p)
        s.commit()
        s.refresh(p)
        for loc in locales:
            s.add(TargetLocale(project_id=p.id, locale=loc))
        doc = Document(
            project_id=p.id,
            filename="batch.txt",
            fmt="txt",
            mode=TransMode.FULL_TEXT,
            skeleton={"units": [{"text": "A whole body of text."}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for loc in locales:
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=loc,
                    order_index=0,
                    source_text="A whole body of text.",
                    target_text="",
                )
            )
        s.commit()
        return p.id, doc.id


def _mk_tm_wf(name):
    """A workflow whose step 0 is tm_match (the FT-incompatible shape)."""
    from app.models import StepType

    with Session(engine) as s:
        w = W.create_workflow(s, name)
        W.add_step(s, w.id, name="TM", step_type=StepType.TM_MATCH)
        W.add_step(s, w.id, name="MT", provider="mock", model="m1", prompt_text="Translate {text}")
        return w.id


def _mk_plain_wf(name):
    """A plain (no tm_match) mock llm_agent workflow — a clean variant / parent."""
    from app.models import StepType

    with Session(engine) as s:
        w = W.create_workflow(s, name)
        W.add_step(
            s,
            w.id,
            name="MT",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            model="m1",
            prompt_text="Translate {text}",
        )
        return w.id


def _mk_group_pair(default_id, default_pair, variant_id, variant_pair, source="en-US"):
    """Assemble a group whose DEFAULT variant is `default_id` (the id a caller names / a
    batch is stamped with) and which also holds `variant_id` for its own pair. Returns the
    group id."""
    with Session(engine) as s:
        grp = W.create_group(s, _uniq("VariantGrp"))
        gid = grp.id
        W.add_to_group(
            s, default_id, gid, source_locale=source, target_locale=default_pair, make_default=True
        )
        W.add_to_group(s, variant_id, gid, source_locale=source, target_locale=variant_pair)
        return gid


def _mk_predates_batch(parent_id, doc_id, locale, member_wf_id, project_id=None):
    """A finished WorkflowBatch stamped with `parent_id` plus one terminal (FAILED) member
    run for `locale` on `member_wf_id` — the shape a batch created BEFORE the guard has. Lets
    a rerun exercise rerun_batch's resolved-plan guard on a real batch. Returns the batch id."""
    from datetime import datetime

    from app.models import BatchStatus, Project

    with Session(engine) as s:
        org_id = None
        if project_id is not None:
            proj = s.get(Project, project_id)
            org_id = getattr(proj, "org_id", None)
        b = WorkflowBatch(
            org_id=org_id,
            workflow_id=parent_id,
            document_id=doc_id,
            requested_locales=[locale],
            status=BatchStatus.FAILED,
            finished_at=datetime.utcnow(),
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        s.add(
            WorkflowRun(
                workflow_id=member_wf_id,
                document_id=doc_id,
                target_locale=locale,
                status=RunStatus.FAILED,
                batch_id=b.id,
                finished_at=datetime.utcnow(),
            )
        )
        s.commit()
        return b.id


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                failures += 1
                import traceback

                print(f"  FAIL {name}: {exc}")
                traceback.print_exc()
    if failures:
        print(f"verify_batch_runs FAILED ({failures})")
        raise SystemExit(1)
    print("BATCH RUNS OK")
