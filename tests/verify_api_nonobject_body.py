"""#513 item 3: non-object JSON request bodies must be a clean 400, never a 500.

The connector translate/sync/retry/Jira/Confluence routes and POST
/api/documents/{id}/translate each did ``payload = await request.json()`` and then
``payload.get(...)`` with no shape check. A body of ``[]`` (list) or ``"x"`` (str)
is valid JSON, so ``payload.get`` raised AttributeError and the route 500'd. The
sibling routes that already guard (api_cli_misc_3.py:980) refuse a non-dict payload
with ``{"ok": False, "error": "payload must be an object."}`` + 400; these routes now
do the same.

Fully offline: the terminal connector store functions are stubbed so an OBJECT body
stays hermetic (no LLM / network). The isinstance guard runs BEFORE those calls, so the
non-object 400 still exercises the real check -- the stubs never short-circuit it.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_nonobj_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Document, Role
from app.services import apikeys, content, security, settings
from app.services.connectors import store as cstore
from tests._api_fixture import user_api_token
from tests._piece_patch import set_pieces

_GUARD_ERROR = "payload must be an object."


def main() -> None:
    init_db()
    # Single-operator mode so the connector role gate is a no-op and the object-body
    # path stays deterministic; the object-authz ring is inert here (auth off).
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()

    api_token = user_api_token()  # admin per-user key, scopes "api"

    # The document route needs a docs-scoped per-user key ("api" alone is a 403).
    doc_user = security.get_by_email("nonobj-docs@example.test")
    if doc_user is None:
        doc_user = security.create_user(
            "nonobj-docs@example.test",
            "Nonobj Docs",
            Role.ADMIN,
            "offline-fixture-password-123",  # pregate: allow-secret offline fixture
        )
    docs_key = apikeys.create_key(doc_user.id, "nonobj docs", scopes="api docs")[1]

    # A real, ENABLED connection (airtable ships enabled) so _conn_gate passes and the
    # route reaches the body-parse the guard protects.
    conn = cstore.create(
        "airtable",
        "nonobj",
        {"base_id": "appX", "table": "T", "source_field": "S", "target_field": "D"},
        {"api_key": "patNONOBJ"},  # pregate: allow-secret fixture
        actor="nonobj",
    )
    cid = conn.id

    # A real project + document for the /api/documents/{id}/translate route.
    with Session(engine) as s:
        proj = content.project_create(
            s,
            "nonobj proj",
            source_locale="en-US",
            target_locales="fr-FR",
            default_provider="mock",
            created_by="admin@example.test",
        )
        doc = Document(
            project_id=proj.id, filename="d.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id

    # raise_server_exceptions=False so any residual AttributeError surfaces as a 500
    # we assert against, rather than escaping as an exception (a clean FAIL before the fix).
    c = TestClient(app, raise_server_exceptions=False)
    ah = {"X-Loc-Token": api_token}
    dh = {"X-Loc-Token": docs_key}

    # Object bodies must stay offline: stub the terminal store fns (the guard is upstream).
    def ok_stub(cid_, **kw):
        return (True, "stub", {})

    set_pieces("app.services.connectors.store", "translate_now", ok_stub)
    set_pieces("app.services.connectors.store", "jira_translate", ok_stub)
    set_pieces("app.services.connectors.store", "confluence_translate", ok_stub)
    set_pieces("app.services.connectors.store", "notion_translate", ok_stub)

    routes = [
        ("sync", f"/api/connectors/{cid}/sync", ah),
        ("retry", f"/api/connectors/{cid}/retry", ah),
        ("jira", f"/api/connectors/{cid}/jira/translate", ah),
        ("confluence", f"/api/connectors/{cid}/confluence/translate", ah),
        ("article(notion)", f"/api/connectors/{cid}/notion/translate", ah),
        ("document", f"/api/documents/{did}/translate", dh),
        # Sibling connector routes in api.py with the same unchecked payload.get().
        ("jira-count", f"/api/connectors/{cid}/jira/count", ah),
        ("notion-resolve", f"/api/connectors/{cid}/notion/resolve-link", ah),
        ("helpscout-resolve", f"/api/connectors/{cid}/helpscout/resolve-link", ah),
        ("wordpress-resolve", f"/api/connectors/{cid}/wordpress/resolve-link", ah),
        ("airtable-webhook-register", f"/api/connectors/{cid}/airtable/webhook/register", ah),
        ("airtable-records", f"/api/connectors/{cid}/airtable/records", ah),
        ("airtable-records-delete", f"/api/connectors/{cid}/airtable/records/delete", ah),
        ("airtable-webhook", f"/api/connectors/{cid}/airtable/webhook", ah),
        ("prune-config", f"/api/connectors/{cid}/prune-config", ah),
    ]

    for name, url, hdrs in routes:
        # A JSON array and a JSON string are both valid JSON but not objects -> 400 JSON,
        # never 500. (Fails before the fix: AttributeError -> 500.)
        for bad in ([], "x"):
            r = c.post(url, headers=hdrs, json=bad)
            assert r.status_code == 400, (name, repr(bad), r.status_code, r.text)
            body = r.json()
            assert body.get("ok") is False, (name, repr(bad), body)
            assert body.get("error"), (name, repr(bad), body)
        # A normal object body behaves as before: the guard lets dicts through, so the
        # response is never the guard's refusal and never a 500.
        obj = c.post(url, headers=hdrs, json={})
        assert obj.status_code != 500, (name, "object body 500", obj.text)
        assert obj.json().get("error") != _GUARD_ERROR, (name, "guard fired on a dict")
    print(
        "  non-object bodies (list, string) -> 400 JSON on every route; object body "
        "still passes the guard"
    )
    print("NONOBJECT BODY OK")


if __name__ == "__main__":
    main()
