"""Offline UX seed: review-runs records (opt-in via fresh_fixture(review_runs=True)).

Documented in: DESIGN.md. Application modules are imported inside the functions, after
fresh_fixture() has pointed the app at its disposable data directory.
"""


# never dispatches a workflow. Kept out of the base seed so other pages' baselines stay put.
def seed_review_runs(client, ids):
    def post(path, data):
        response = client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 303, (path, response.status_code, response.text[:200])
        return response.headers["location"].split("?")[0].rstrip("/").split("/")[-1]

    from datetime import UTC, datetime

    from sqlmodel import Session, select

    from app.db import engine
    from app.models import LQAResult, StepRun, WorkflowBatch, WorkflowRun, WorkflowStep
    from app.services import security, settings

    settings.set_many({"queries_enabled": "1", "worklist_enabled": "1"})
    settings.invalidate_cache()
    with Session(engine) as session:
        workflow_id = int(ids["workflow"])
        step = session.exec(
            select(WorkflowStep).where(WorkflowStep.workflow_id == workflow_id)
        ).first()
        step.assignee_email = security.SYSTEM.email
        session.add(step)
        batch = WorkflowBatch(
            workflow_id=int(ids["workflow"]),
            document_id=int(ids["document"]),
            requested_locales=["fr-FR", "de-DE", "es-ES", "it-IT"],
            finished_at=datetime.now(UTC).replace(tzinfo=None),
            status="partial",
        )
        session.add(batch)
        session.commit()
        session.refresh(batch)
        batch_id = batch.id
        run_ids = []
        for locale, status in [("fr-FR", "paused"), ("de-DE", "completed"), ("es-ES", "failed")]:
            run = WorkflowRun(
                workflow_id=int(ids["workflow"]),
                document_id=int(ids["document"]),
                target_locale=locale,
                status=status,
                batch_id=batch_id,
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            run_ids.append(run.id)
            for output in [
                "Bienvenue dans Atlas.\n\nCommencez votre projet.",
                "Bienvenue sur Atlas.\n\nCommencez votre premier projet.",
            ]:
                session.add(
                    StepRun(
                        workflow_run_id=run.id,
                        step_id=step.id,
                        status=status,
                        input_text=output,
                        output_text=output,
                        started_at=datetime.now(UTC).replace(tzinfo=None),
                        latency_ms=1200,
                    )
                )
        session.add(
            LQAResult(
                document_id=int(ids["document"]),
                dimension="accuracy",
                severity="major",
                target_locale="fr-FR",
                note="Check product terminology.",
            )
        )
        session.commit()
    query = post(
        "/queries",
        {
            "project_id": ids["project"],
            "subject": "Atlas product terminology",
            "body": "Should the product name remain unchanged?",
        },
    )
    post(
        f"/queries/{query}/posts",
        {"body": "Keep Atlas unchanged in every language.", "answer": "1"},
    )
    return {"run": run_ids[0], "batch": batch_id, "query": query}
