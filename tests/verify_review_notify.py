"""Human-review assignment notification email (Feature B, offline).

Proves, hermetically (throwaway LOC_TMS_DATA_DIR + cleared provider keys force
MockProvider; mailer.send monkeypatched to CAPTURE -- no real SMTP is ever
touched; ASCII prints only):

  - a workflow run that PAUSES at a human_review step WITH assignee_email calls
    mailer.send EXACTLY ONCE, to that assignee, with a subject naming the doc +
    locale pair and a body carrying the language pair + project + due-date line +
    the DIRECT LINK (app_base_url set) OR the fallback line (app_base_url unset);
  - NO send when the human step has no assignee_email;
  - NO send when notify_on_review_assigned = 0;
  - graceful no-op (no crash, returns False) when SMTP is unconfigured;
  - the run summary is BYTE-IDENTICAL for a run WITHOUT an assignee vs the same
    workflow before this feature existed (the additive side effect does not alter
    the event stream / summary);
  - a run that resumes + pauses AGAIN at a SECOND human step notifies AGAIN (once
    per pause), while an observe_run REPLAY does NOT re-fire.
"""

import os
import sys
from datetime import datetime, timedelta
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_revnotif_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Project,
    StepType,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import mailer, notifications, orchestrator, settings

SENT: list[dict] = []


def _capture(to, subject, body):
    SENT.append({"to": to, "subject": subject, "body": body})
    return True  # pretend SMTP is configured + delivered


def docx_from(paragraphs) -> bytes:
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def new_project(client, name, targets="fr-FR", source="en-US"):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": source,
            "target_locales": targets,
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def upload(client, pid, paragraphs, mode="segmented"):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": mode},
        files={
            "file": (
                "d.docx",
                docx_from(paragraphs),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def add_step(
    session,
    wf_id,
    order,
    name,
    step_type=StepType.LLM_AGENT,
    provider="claude",
    prompt_text="",
    assignee_email="",
):
    session.add(
        WorkflowStep(
            workflow_id=wf_id,
            order_index=order,
            name=name,
            step_type=step_type,
            provider=provider,
            prompt_text=prompt_text,
            retry_max=1,
            backoff_base=0.0,
            assignee_email=assignee_email,
        )
    )
    session.commit()


def main() -> None:
    init_db()
    settings.set_many(
        {"org_name": "Acme Loc", "notify_on_review_assigned": "1", "app_base_url": ""}
    )
    client = TestClient(app_or_none())

    orig_send = mailer.send
    mailer.send = _capture
    try:
        # =================================================================
        # (1) pause WITH assignee + app_base_url set -> ONE email, right
        #     recipient/subject, body carries pair+project+due+DIRECT LINK
        # =================================================================
        settings.set_many({"app_base_url": "https://tms.example.com"})
        due = datetime.utcnow() + timedelta(days=2)
        pid = new_project(client, "Review WF")
        # set a due date on the project so the body's due-line is exercised
        with Session(engine) as s:
            pr = s.exec(select(Project).where(Project.id == pid)).first()
            pr.due_date = due
            s.add(pr)
            s.commit()
        doc = upload(client, pid, ["Alpha source.", "Beta source."])
        with Session(engine) as s:
            wf = Workflow(name="Rev wf", scope="global")
            s.add(wf)
            s.commit()
            s.refresh(wf)
            wf_id = wf.id
            add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
            add_step(
                s,
                wf_id,
                1,
                "Linguist",
                step_type=StepType.HUMAN_REVIEW,
                assignee_email="reviewer@acme.example",
            )
            add_step(s, wf_id, 2, "Finalizer", prompt_text="Finalize: {text}")

        SENT.clear()
        with Session(engine) as s:
            paused = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert paused["status"] == "paused", paused
        assert len(SENT) == 1, ("exactly ONE email on a real pause", SENT)
        m = SENT[0]
        assert m["to"] == "reviewer@acme.example", m
        assert "Acme Loc" in m["subject"] and "Review assigned" in m["subject"], m
        assert "d.docx" in m["subject"], m
        assert "en-US -> fr-FR" in m["subject"], ("locale pair in subject", m["subject"])
        assert "en-US -> fr-FR" in m["body"], ("language pair in body", m["body"])
        assert "Review WF" in m["body"], ("project name in body", m["body"])
        assert "Linguist" in m["body"], ("step/role in body", m["body"])
        assert "Due date:" in m["body"] and "Due in" in m["body"], (
            "due-date line in body",
            m["body"],
        )
        run_id = paused["run_id"]
        assert f"https://tms.example.com/runs/{run_id}" in m["body"], (
            "DIRECT LINK to the run view",
            m["body"],
        )
        print(
            "  (1) pause WITH assignee + base URL: ONE email, right recipient/subject, "
            "body has pair+project+due+direct link"
        )

        # =================================================================
        # (2) app_base_url UNSET -> the fallback line, no absolute link
        # =================================================================
        settings.set_many({"app_base_url": ""})
        pid2 = new_project(client, "Review WF2")
        doc2 = upload(client, pid2, ["Gamma source."])
        with Session(engine) as s:
            wf2 = Workflow(name="Rev wf2", scope="global")
            s.add(wf2)
            s.commit()
            s.refresh(wf2)
            wf2_id = wf2.id
            add_step(s, wf2_id, 0, "Translator", prompt_text="Translate: {text}")
            add_step(
                s,
                wf2_id,
                1,
                "Reviewer",
                step_type=StepType.HUMAN_REVIEW,
                assignee_email="rev2@acme.example",
            )
        SENT.clear()
        with Session(engine) as s:
            orchestrator.run_workflow(s, wf2_id, doc2, "fr-FR")
        assert len(SENT) == 1, SENT
        b = SENT[0]["body"]
        # SPEC-wave3 §10 text-truth fix: the fallback no longer says "review queue" (there
        # was none); it now points reviewers at the My work page (LC-5, Train D). No live
        # /my-work URL is ever emailed (H-3) — the page is named in prose, the absolute
        # link stays the run link, absent here since app_base_url is unset.
        assert "my work" in b.lower(), ("fallback My-work line", b)
        assert "review queue" not in b.lower(), ("stale 'review queue' text", b)
        assert "http" not in b, ("no absolute link when app_base_url is unset", b)
        assert "Due date: none set" in b, ("no-due-date line", b)
        print(
            "  (2) app_base_url unset: fallback 'My work' line, no absolute link, "
            "'none set' due line"
        )

        # =================================================================
        # (2a) FIX 3 (coverage gap): every OTHER case in this suite uploads
        #      mode="segmented", so only the SEGMENTED pause site (orchestrator
        #      ~491) was exercised; the FULL_TEXT pause site (~232) had ZERO
        #      coverage. Same shape as case (1) but mode="full_text" -- proves
        #      the notification fires exactly once from that site too.
        # =================================================================
        settings.set_many({"app_base_url": "https://tms.example.com"})
        pid_ft = new_project(client, "Review WF Full Text")
        doc_ft = upload(client, pid_ft, ["Iota source."], mode="full_text")
        with Session(engine) as s:
            wf_ft = Workflow(name="Rev wf full text", scope="global")
            s.add(wf_ft)
            s.commit()
            s.refresh(wf_ft)
            wf_ft_id = wf_ft.id
            add_step(s, wf_ft_id, 0, "Translator", prompt_text="Translate: {text}")
            add_step(
                s,
                wf_ft_id,
                1,
                "Linguist",
                step_type=StepType.HUMAN_REVIEW,
                assignee_email="ft-reviewer@acme.example",
            )
            add_step(s, wf_ft_id, 2, "Finalizer", prompt_text="Finalize: {text}")
        SENT.clear()
        with Session(engine) as s:
            paused_ft = orchestrator.run_workflow(s, wf_ft_id, doc_ft, "fr-FR")
        assert paused_ft["status"] == "paused", paused_ft
        assert len(SENT) == 1, ("exactly ONE email on a FULL_TEXT pause", SENT)
        m_ft = SENT[0]
        assert m_ft["to"] == "ft-reviewer@acme.example", m_ft
        assert "Review assigned" in m_ft["subject"], m_ft
        print(
            "  (2a) FIX 3: a FULL_TEXT-mode document's human_review pause "
            "notifies exactly once too (was zero coverage before)"
        )

        # =================================================================
        # (2b) adversarial-review FIX 1 + FIX 2: _due_line agrees with the
        #      dashboard/project-page badge (content.due_status) for BOTH a
        #      terminal-status project past its due date (never "OVERDUE") and
        #      the day count it reports for an active due-soon project.
        # =================================================================
        from app.models import ProjectStatus
        from app.services import content

        now = datetime(2026, 6, 1, 12, 0, 0)

        # A COMPLETED project whose due date is in the past: due_status says
        # None (never overdue for finished work) -> the email line must NOT
        # say OVERDUE either, matching the badge exactly.
        p_done = Project(
            name="Finished Past Due",
            due_date=now - timedelta(days=5),
            status=ProjectStatus.COMPLETED,
        )
        assert content.due_status(p_done, now) is None, (
            "sanity: badge must say None for a completed project past due"
        )
        line_done = notifications._due_line(p_done, now)
        assert "OVERDUE" not in line_done, (
            "a finished project's email due-line must never say OVERDUE "
            "(must agree with the UI badge)",
            line_done,
        )
        assert "2026-05-27" in line_done, line_done
        print(
            "  (2b-i) FIX 1: _due_line for a COMPLETED past-due project says "
            "no OVERDUE -- agrees with content.due_status (None)"
        )

        # Same check for CLOSED and CANCELLED (the other two HIDDEN_BY_DEFAULT
        # / terminal statuses due_status guards against).
        for st in (ProjectStatus.CLOSED, ProjectStatus.CANCELLED):
            p_term = Project(name=f"Terminal {st}", due_date=now - timedelta(days=1), status=st)
            assert content.due_status(p_term, now) is None, st
            assert "OVERDUE" not in notifications._due_line(p_term, now), (
                st,
                notifications._due_line(p_term, now),
            )
        print("  (2b-ii) FIX 1: same guard holds for closed + cancelled projects")

        # An ACTIVE project run past-due at a human_review step: due_status DOES
        # say "overdue" and the email line MUST say OVERDUE (the guard is
        # status-specific, not a blanket suppression).
        p_active_over = Project(
            name="Active Overdue",
            due_date=now - timedelta(days=1),
            status=ProjectStatus.IN_TRANSLATION,
        )
        assert content.due_status(p_active_over, now) == "overdue"
        assert "OVERDUE" in notifications._due_line(p_active_over, now), (
            "an ACTIVE overdue project must still say OVERDUE in the email"
        )
        print(
            "  (2b-iii) FIX 1: an ACTIVE overdue project still says OVERDUE "
            "(the guard only exempts terminal statuses)"
        )

        # FIX 2: day-count agreement. A project due in exactly 2 calendar days
        # from an intra-day `now` must be classified the same way (due_soon,
        # since 2 < DUE_SOON_DAYS=3) AND the email's day count must read "2",
        # not "1" from a naive floor of the fractional-day delta.
        p_soon2 = Project(
            name="Soon In Two",
            due_date=now + timedelta(days=2),
            status=ProjectStatus.IN_TRANSLATION,
        )
        assert content.due_status(p_soon2, now) == "due_soon"
        assert content.days_until_due(p_soon2.due_date, now) == 2
        line_soon2 = notifications._due_line(p_soon2, now)
        assert "Due in 2 days" in line_soon2, (
            "the email day count must match content.days_until_due (2), not a floored 1",
            line_soon2,
        )
        print(
            "  (2b-iv) FIX 2: badge (due_soon) and email ('Due in 2 days') "
            "agree on the day count via the shared days_until_due helper"
        )

        # =================================================================
        # (3) NO assignee -> no send; and the run summary is BYTE-IDENTICAL
        #     to the same run BEFORE the feature (no assignee = no side effect)
        # =================================================================
        pid3 = new_project(client, "No Assignee")
        doc3 = upload(client, pid3, ["Delta source.", "Epsilon source."])
        with Session(engine) as s:
            wf3 = Workflow(name="No assignee wf", scope="global")
            s.add(wf3)
            s.commit()
            s.refresh(wf3)
            wf3_id = wf3.id
            add_step(s, wf3_id, 0, "Translator", prompt_text="Translate: {text}")
            add_step(
                s, wf3_id, 1, "Human", step_type=StepType.HUMAN_REVIEW, assignee_email=""
            )  # no assignee
        SENT.clear()
        with Session(engine) as s:
            noass = orchestrator.run_workflow(s, wf3_id, doc3, "fr-FR")
        assert SENT == [], ("no assignee -> no email", SENT)
        assert noass["status"] == "paused", noass

        # BYTE-IDENTICAL proof: run the SAME no-assignee workflow on a fresh doc
        # with the notification feature entirely DISABLED (the pre-feature world),
        # then RE-ENABLED, and assert the two summaries are identical after
        # normalizing only the run_id (which legitimately differs per run). The
        # additive side effect must not alter the event stream / summary at all.
        def _norm(sm):
            sm = dict(sm)
            sm.pop("run_id", None)
            for st in sm.get("steps", []):
                st.pop("run_id", None)
            return sm

        doc3b = upload(client, pid3, ["Delta source.", "Epsilon source."])
        settings.set_many({"notify_on_review_assigned": "0"})  # pre-feature world
        with Session(engine) as s:
            base = orchestrator.run_workflow(s, wf3_id, doc3b, "fr-FR")
        doc3c = upload(client, pid3, ["Delta source.", "Epsilon source."])
        settings.set_many({"notify_on_review_assigned": "1"})  # feature back on
        SENT.clear()
        with Session(engine) as s:
            withft = orchestrator.run_workflow(s, wf3_id, doc3c, "fr-FR")
        assert SENT == [], ("no assignee -> still no email with the feature on", SENT)
        assert _norm(base) == _norm(withft), (
            "run summary must be byte-identical with the feature on vs off for a no-assignee run",
            _norm(base),
            _norm(withft),
        )
        assert "notified" not in withft and "email" not in withft, (
            "no notification residue in the summary",
            withft,
        )
        print(
            "  (3) no assignee: NO email; run summary byte-identical feature-on vs "
            "feature-off (no side effect on the stream)"
        )

        # =================================================================
        # (4) notify_on_review_assigned = 0 kill switch -> no send
        # =================================================================
        settings.set_many({"notify_on_review_assigned": "0"})
        pid4 = new_project(client, "Kill Switch")
        doc4 = upload(client, pid4, ["Zeta source."])
        with Session(engine) as s:
            wf4 = Workflow(name="Kill wf", scope="global")
            s.add(wf4)
            s.commit()
            s.refresh(wf4)
            wf4_id = wf4.id
            add_step(s, wf4_id, 0, "Translator", prompt_text="Translate: {text}")
            add_step(
                s,
                wf4_id,
                1,
                "Human",
                step_type=StepType.HUMAN_REVIEW,
                assignee_email="rev4@acme.example",
            )
        SENT.clear()
        with Session(engine) as s:
            orchestrator.run_workflow(s, wf4_id, doc4, "fr-FR")
        assert SENT == [], ("kill switch must suppress the email", SENT)
        settings.set_many({"notify_on_review_assigned": "1"})
        print("  (4) notify_on_review_assigned=0: no email")

        # =================================================================
        # (5) resume + SECOND human step -> notifies AGAIN (once per pause)
        # =================================================================
        settings.set_many({"app_base_url": "https://tms.example.com"})
        pid5 = new_project(client, "Two Humans")
        doc5 = upload(client, pid5, ["Eta source.", "Theta source."])
        with Session(engine) as s:
            wf5 = Workflow(name="Two human wf", scope="global")
            s.add(wf5)
            s.commit()
            s.refresh(wf5)
            wf5_id = wf5.id
            add_step(s, wf5_id, 0, "Translator", prompt_text="Translate: {text}")
            add_step(
                s,
                wf5_id,
                1,
                "First",
                step_type=StepType.HUMAN_REVIEW,
                assignee_email="first@acme.example",
            )
            add_step(
                s,
                wf5_id,
                2,
                "Second",
                step_type=StepType.HUMAN_REVIEW,
                assignee_email="second@acme.example",
            )
        SENT.clear()
        with Session(engine) as s:
            p1 = orchestrator.run_workflow(s, wf5_id, doc5, "fr-FR")
        assert p1["status"] == "paused" and p1["paused_step"] == 1, p1
        assert len(SENT) == 1 and SENT[0]["to"] == "first@acme.example", SENT
        # resume past the FIRST human step -> the run pauses at the SECOND
        edited = orchestrator.SEG_JOINER.join(["EDIT ETA", "EDIT THETA"])
        with Session(engine) as s:
            p2 = orchestrator.resume_workflow(s, p1["run_id"], "fr-FR", edited)
        assert p2["status"] == "paused" and p2["paused_step"] == 2, p2
        assert len(SENT) == 2 and SENT[1]["to"] == "second@acme.example", (
            "second human step must notify again (once per pause)",
            SENT,
        )
        print(
            "  (5) resume + second human step: notifies AGAIN (once per pause), "
            "to the second assignee"
        )

        # =================================================================
        # (6) observe_run REPLAY does NOT re-fire the notification
        # =================================================================
        # The run is paused at step 2 (from case 5). Draining observe_run replays
        # the recorded events (run_started, steps, paused, closed) WITHOUT creating
        # a new PAUSED StepRun -> no notification.
        SENT.clear()
        for _run, _ev in orchestrator.observe_run(p1["run_id"], poll_interval=0.01, max_seconds=5):
            if _ev.get("type") == "closed":
                break
        assert SENT == [], ("observe_run replay must NOT re-fire the notification", SENT)
        print(
            "  (6) observe_run replay: NO re-fire (StepRun-creation is the "
            "once-per-pause chokepoint)"
        )
    finally:
        mailer.send = orig_send

    # =================================================================
    # (7) unconfigured SMTP -> graceful no-op (returns False, never raises)
    # =================================================================
    assert not mailer.configured(), "test assumes SMTP unconfigured"
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).order_by(WorkflowRun.id.desc())).first()
        step = s.exec(select(WorkflowStep).where(WorkflowStep.assignee_email != "")).first()
    sent = notifications.on_human_review_paused(run, step)
    assert sent is False, "unconfigured SMTP must no-op silently (return False)"
    print("  (7) unconfigured SMTP: on_human_review_paused returns False, never raises")

    print("REVIEW NOTIFY OK")


def app_or_none():
    from app.main import app

    return app


if __name__ == "__main__":
    main()
