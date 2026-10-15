"""Completion email notifications (Phase B chunk 5, offline).

  - a COMPLETED translate_document job with an email created_by -> success email
    (right recipient, subject, and a body naming the document + language + engine)
  - a FAILED job -> failure email whose body carries the error + a resubmit prompt
    (and does NOT falsely promise an automatic retry)
  - connector jobs send NOTHING (scope is translate_document only)
  - a job with a non-email created_by (system/scheduler/pseudo-actor) sends nothing
  - unconfigured SMTP is a silent no-op (mailer.send returns False), never raises
  - a non-numeric smtp_port is a logged failure too (returns False, never a raise) (#454)
  - notify_on_job_finish=0 kill switch suppresses everything
  - end-to-end: jobs._execute calls on_job_finished at the terminal chokepoint, and
    a raising mailer.send NEVER crashes the worker (the job still completes)

mailer.send is monkeypatched to capture calls; no real SMTP is touched.
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_notif_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document, Job, JobStatus, Project, Setting
from app.services import mailer, notifications, settings

SENT: list[dict] = []


def _capture(to, subject, body):
    SENT.append({"to": to, "subject": subject, "body": body})
    return True  # pretend SMTP is configured + delivered


def _seed_job(
    kind, status, created_by, *, doc_id=None, locale="fr-FR", result=None, error=""
) -> Job:
    with Session(engine) as s:
        j = Job(
            kind=kind,
            status=status,
            created_by=created_by,
            payload={"document_id": doc_id, "locale": locale} if doc_id else {"locale": locale},
            result=result or {},
            error=error,
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        return j


def main() -> None:
    init_db()
    settings.set_many({"org_name": "Acme Loc", "notify_on_job_finish": "1"})

    with Session(engine) as s:
        p = Project(name="P", source_locale="en-US", created_by="u@example.com")
        s.add(p)
        s.commit()
        s.refresh(p)
        doc = Document(
            project_id=p.id, filename="report.docx", fmt="docx", created_by="u@example.com"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        doc_id = doc.id

    orig_send = mailer.send
    mailer.send = _capture
    try:
        # --- success body -------------------------------------------------
        SENT.clear()
        job = _seed_job(
            "translate_document",
            JobStatus.COMPLETED,
            "u@example.com",
            doc_id=doc_id,
            locale="fr-FR",
            result={"provider_used": {"claude": 3}},
        )
        sent = notifications.on_job_finished(job)
        assert sent is True and len(SENT) == 1, SENT
        m = SENT[0]
        assert m["to"] == "u@example.com", m
        assert "ready" in m["subject"].lower() and "report.docx" in m["subject"], m
        assert "report.docx" in m["body"] and "fr-FR" in m["body"], m["body"]
        assert "claude" in m["body"], ("engine should be named", m["body"])
        assert "Acme Loc" in m["subject"], "org name should prefix the subject"
        print("  success: COMPLETED job -> email naming document + language + engine")

        # --- failure body -------------------------------------------------
        SENT.clear()
        job = _seed_job(
            "translate_document",
            JobStatus.FAILED,
            "u@example.com",
            doc_id=doc_id,
            locale="de-DE",
            error="provider timeout",
        )
        assert notifications.on_job_finished(job) is True and len(SENT) == 1, SENT
        m = SENT[0]
        assert "failed" in m["subject"].lower(), m
        assert "provider timeout" in m["body"], ("error should appear in body", m["body"])
        assert "retry" not in m["body"].lower(), (
            "failure body must NOT promise a retry",
            m["body"],
        )
        assert "resubmit" in m["body"].lower(), (
            "failure body should tell the user to resubmit",
            m["body"],
        )
        print(
            "  failure: FAILED job -> email with the error + a resubmit prompt (no false retry promise)"  # noqa: E501
        )

        # --- connector jobs send NOTHING ----------------------------------
        SENT.clear()
        job = _seed_job("connector_translate", JobStatus.COMPLETED, "u@example.com")
        assert notifications.on_job_finished(job) is False and SENT == [], SENT
        print("  scope: connector_translate jobs send no email")

        # --- non-email created_by sends nothing ---------------------------
        SENT.clear()
        for actor in ("scheduler", "system", "airtable-automation", "companion-app", ""):
            job = _seed_job("translate_document", JobStatus.COMPLETED, actor, doc_id=doc_id)
            assert notifications.on_job_finished(job) is False, actor
        assert SENT == [], SENT
        print("  scope: non-email created_by (system/scheduler/pseudo-actor) sends nothing")

        # --- kill switch --------------------------------------------------
        SENT.clear()
        settings.set_many({"notify_on_job_finish": "0"})
        job = _seed_job("translate_document", JobStatus.COMPLETED, "u@example.com", doc_id=doc_id)
        assert notifications.on_job_finished(job) is False and SENT == [], SENT
        settings.set_many({"notify_on_job_finish": "1"})
        print("  kill switch: notify_on_job_finish=0 suppresses everything")
    finally:
        mailer.send = orig_send

    # --- unconfigured SMTP is a silent no-op (real mailer.send path) -------
    # No smtp_host set -> mailer.configured() False -> send returns False, no raise.
    assert not mailer.configured(), "test assumes SMTP unconfigured"
    job = _seed_job("translate_document", JobStatus.COMPLETED, "u@example.com", doc_id=doc_id)
    assert notifications.on_job_finished(job) is False, "unconfigured SMTP must no-op silently"
    print("  no-op: unconfigured SMTP returns False, never raises")

    # --- #454: a garbage smtp_port fails the send, it never raises ----------
    # send() is documented never to raise, yet the port used to be int()'d AHEAD of the
    # try, so a stored "abc" raised ValueError straight out of the call: an invite request
    # 500'd after its commit and never reached its audit row, and every guarded caller
    # (MFA, notifications) dropped its mail in silence. The row is seeded directly into
    # the Setting table because settings.set_many now REFUSES such a value (the #454 save
    # door, proven in verify_api_r3_config_credentials) -- this is the legacy row that
    # door cannot retroactively clean. The host is never dialled: the port fails first.
    with Session(engine) as s:
        for key, val in (("smtp_host", "smtp.invalid.example"), ("smtp_port", "abc")):
            row = s.get(Setting, key)
            if row is None:
                row = Setting(key=key, value=val)
            else:
                row.value = val
            s.add(row)
        s.commit()
    settings.invalidate_cache()
    try:
        assert mailer.configured(), "test assumes smtp_host is set"
        assert mailer.send("u@example.com", "s", "b") is False, (
            "#454: a non-numeric smtp_port must return False, never raise"
        )
        print("  #454: smtp_port='abc' -> send() returns False (logged failure, no raise)")
    finally:
        settings.set_many({"smtp_host": "", "smtp_port": ""})
        settings.invalidate_cache()

    # --- end-to-end via jobs._execute: a raising mailer.send never crashes --
    # the worker; the job still reaches COMPLETED. Drive one real translate job.
    from app.services import jobs
    from app.services.providers import Provider, register_provider

    class LiveStub(Provider):
        name = "claude"

        def complete(self, prompt, system=None, task=None, payload=None, **kw):
            if task == "translate":
                payload = payload or {}
                return f"[{payload.get('target', 'xx')}] {payload.get('text', '')}"
            return (prompt or "").strip()

    # seed a real doc+segment so the translate handler has work
    with Session(engine) as s:
        from app.models import Segment, TargetLocale

        d = Document(project_id=1, filename="live.txt", fmt="txt", created_by="u@example.com")
        s.add(d)
        s.commit()
        s.refresh(d)
        live_doc = d.id
        s.add(
            Segment(
                document_id=live_doc,
                target_locale="fr-FR",
                order_index=0,
                source_text="Hello",
                target_text="",
            )
        )
        s.add(TargetLocale(project_id=1, locale="fr-FR"))
        s.commit()

    def _boom(to, subject, body):
        raise RuntimeError("SMTP exploded")

    register_provider("claude", LiveStub())
    orig_send = mailer.send
    mailer.send = _boom
    try:
        jid = jobs.submit(
            "translate_document",
            {"document_id": live_doc, "locale": "fr-FR", "force_pii": True},
            actor="u@example.com",
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            j = jobs.get(jid)
            if j and j.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
            time.sleep(0.05)
        assert j.status == JobStatus.COMPLETED, (
            "worker must complete despite mail crash",
            j.status,
            j.error,
        )
        print("  worker-safe: a raising mailer.send does not crash the worker (job COMPLETED)")
    finally:
        mailer.send = orig_send
        from app.services import providers as _prov

        _prov._OVERRIDES.pop("claude", None)

    print("NOTIFICATIONS OK")


if __name__ == "__main__":
    main()
