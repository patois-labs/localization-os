"""Offline UX seed: disconnected connectors and vendors (opt-in, connections pages).

Documented in: DESIGN.md. Application modules are imported inside the functions, after
fresh_fixture() has pointed the app at its disposable data directory.
"""


def seed_connections(post, workflow, document):
    """A disconnected connector, two vendors and inert dispatch rows; nothing is contacted."""
    # disconnected fixtures, no test/sync/dispatch calls.
    connection = post(
        "/connectors/airtable",
        {
            "name": "Northstar product documentation and international launch content",
            "base_id": "appOfflineFixture",
            "table_id": "tblOfflineFixture",
            "source_field": "Launch documentation",
            "customer_id": "1",
        },
    )
    vendor = post(
        "/vendors",
        {
            "name": "Northstar multilingual editorial and localization partners",
            "description": "Offline review fixture",
            "protocol": "manual",
        },
    )
    post(f"/vendors/{vendor}/link-customer", {"customer_id": "1"})
    vendor_work = post("/vendors", {"name": "Atlas review delivery fixture", "protocol": "manual"})
    post(f"/vendors/{vendor_work}/link-customer", {"customer_id": "1"})
    # Persist inert records directly; never dispatch a workflow or contact a vendor.
    from sqlmodel import Session

    from app.db import engine
    from app.models import VendorDispatch, WorkflowRun

    with Session(engine) as session:
        run = WorkflowRun(
            workflow_id=int(workflow),
            document_id=int(document),
            target_locale="fr-FR",
            status="completed",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        for state in ("dispatched", "quoted", "returned"):
            session.add(
                VendorDispatch(
                    vendor_id=int(vendor_work),
                    workflow_run_id=run.id,
                    document_id=int(document),
                    customer_id=1,
                    target_locale="fr-FR",
                    status=state,
                    segment_count=2,
                    protocol="manual",
                    contract_ids={"dispatched": [1, 2], "returned": []},
                )
            )
        session.commit()
    return {"connection": connection, "vendor": vendor, "vendor_work": vendor_work}
