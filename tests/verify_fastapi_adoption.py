"""FastAPI/Starlette route, template, form, and response regression guards."""

import ast
import inspect
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from tests._bootstrap import init_env
from tests._piece_patch import patch_pieces

init_env("loctms_fastapi_adoption_")

import starlette
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from sqlmodel import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Mount

from app.db import engine, init_db
from app.main import _http_error, app
from app.models import Vendor
from app.web import admin, vendors, workflows
from app.web._routes import real_api_routes, walk_routes
from app.web.home import api_endpoint_count
from tests._routes import mount_problems

# 2026-09-14: SRP splits moved template-rendering routes into sibling pieces
_TEMPLATE_FILES = {
    "app/web/admin.py",
    "app/web/admin_jev.py",
    "app/web/admin_part2.py",
    "app/web/auth.py",
    # The TOTP train's two-step sign-in pages (request-first calls, the 1.x contract).
    "app/web/auth_mfa.py",
    "app/web/connectors.py",
    "app/web/notify_routes.py",
    "app/web/connectors_common.py",
    "app/web/content_types.py",
    "app/web/custom_fields.py",
    "app/web/deps.py",
    "app/web/documents.py",
    "app/web/documents_document_level_context_o.py",
    "app/web/documents_tm_only_delivery_empty_g.py",
    "app/web/enterprise.py",
    "app/web/exports.py",
    "app/web/glossaries_approval_can_tune_ai.py",
    "app/web/help.py",
    "app/web/home.py",
    "app/web/images.py",
    "app/web/jobs_ui.py",
    "app/web/keys_hub.py",
    "app/web/keys_hub2.py",
    "app/web/mcp_oauth_as.py",
    "app/web/project_automation.py",
    "app/web/projects.py",
    "app/web/projects_project_rename_delete_sp.py",
    "app/web/projects_projects.py",
    "app/web/query_threads.py",
    "app/web/routing.py",
    "app/web/search.py",
    "app/web/style_guides.py",
    "app/web/tm.py",
    "app/web/vendors.py",
    "app/web/workflows.py",
    "app/web/workflows_part2.py",
    "app/web/workflows_part3.py",
    "app/web/worklist.py",
    "tools/vendor_studio/vstudio/app.py",
}


def test_starlette_1x_template_contract():
    assert starlette.__version__ == "1.7.0"
    params = tuple(inspect.signature(Jinja2Templates.TemplateResponse).parameters)
    assert params[:3] == ("self", "request", "name"), params

    calls = []
    tracked = (
        subprocess.check_output(["git", "ls-files", "-z", "--", "*.py"], cwd=ROOT)
        .decode("utf-8")
        .split("\0")
    )
    for relative in sorted(filter(None, tracked)):
        path = ROOT / relative
        if "tests" in Path(relative).parts:
            continue
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "TemplateResponse"
            ):
                continue
            relative = path.relative_to(ROOT).as_posix()
            first = node.args[0] if node.args else None
            calls.append((relative, node.lineno, ast.unparse(first) if first else "<missing>"))
    assert {path for path, _line, _first in calls} == _TEMPLATE_FILES, calls
    legacy_calls = [call for call in calls if call[2] != "request"]
    assert not legacy_calls, legacy_calls


def schema_pairs(application):
    return {
        (method.upper(), path)
        for path, operations in application.openapi()["paths"].items()
        for method in operations
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "TRACE"}
    }


def test_routes():
    # Independent oracle on whichever FastAPI version runs this suite.
    advertised = {
        (method, route.path)
        for route in walk_routes(app)
        if getattr(route, "include_in_schema", False)
        for method in getattr(route, "methods", ()) or ()
        if method not in {"HEAD", "OPTIONS"}
    }
    assert advertised == schema_pairs(app)
    assert api_endpoint_count(app) == len(real_api_routes(app)) > 100

    fixture = FastAPI()
    parent, child = APIRouter(), APIRouter()

    @child.get("/item")
    def item():
        return {"ok": True}

    @child.post("/hidden", include_in_schema=False)
    def hidden():
        return {"ok": True}

    parent.include_router(child, prefix="/nested")
    fixture.include_router(parent, prefix="/api/one")
    fixture.include_router(parent, prefix="/api/two")
    expected = {
        (method, f"/api/{prefix}/nested/{suffix}")
        for prefix in ("one", "two")
        for method, suffix in (("GET", "item"), ("POST", "hidden"))
    }
    assert real_api_routes(fixture) == expected
    assert schema_pairs(fixture) == {pair for pair in expected if pair[0] == "GET"}
    for method, path in expected:
        assert TestClient(fixture).request(method, path).status_code == 200
    fixture.mount("/api/mounted", FastAPI())
    assert mount_problems(fixture)
    assert any(isinstance(route, Mount) for route in walk_routes(fixture))


def test_forms():
    # Use the actual handlers with their declared dependencies. Isolate authorization
    # and service writes so the assertions inspect the exact command reaching services.
    fixture = FastAPI()
    fixture.add_exception_handler(StarletteHTTPException, _http_error)
    fixture.include_router(workflows.router)
    fixture.include_router(admin.router)
    fixture.include_router(vendors.router)
    client = TestClient(fixture)
    for multipart in (False, True):

        def post(path, values, multipart=multipart):
            if multipart:
                return client.post(
                    path,
                    files={key: (None, value) for key, value in values.items()},
                    follow_redirects=False,
                )
            return client.post(path, data=values, follow_redirects=False)

        with (
            patch_pieces("app.web.workflows", "_ai_block", return_value=(None, None)),
            patch_pieces("app.web.workflows", "_wf_or_404", return_value=(None, None)),
            patch.object(workflows.wf_service, "reorder_step") as move,
        ):
            path = "/workflows/1/steps/2/move"
            assert post(path, {"position": "0"}).status_code == 303
            assert "parallel_group" not in move.call_args.kwargs
            assert post(path, {"position": "0", "parallel_group": ""}).status_code == 303
            assert move.call_args.kwargs["parallel_group"] is None
            assert post(path, {"position": "0", "parallel_group": "lane"}).status_code == 303
            assert move.call_args.kwargs["parallel_group"] == "lane"

        with (
            patch_pieces("app.web.admin", "_admin_block", return_value=(None, None)),
            patch.object(admin.settings, "set_many") as save,
        ):
            assert post("/settings", {"org_name": "Example"}).status_code == 303
            save.assert_called_once()
            save.reset_mock()
            assert (
                post(
                    "/settings", {"org_name": "Example", "prompt_tap_envelopes_enabled": ""}
                ).status_code
                == 400
            )
            save.assert_not_called()

        with (
            patch_pieces("app.web.admin", "_ai_block", return_value=(None, None)) as auth,
            patch.object(admin.settings, "set_many") as save,
            patch.object(admin.audit, "log"),
        ):
            # Existing non-empty Form default contract, including supplied empty.
            assert post("/setup-ai/tm", {"tm_context_floor": ""}).status_code == 303
            assert save.call_args.args[0]["tm_context_floor"] == "95"
            save.reset_mock()
            auth.reset_mock()
            # Required empty text is folded to missing before the handler runs.
            response = post("/setup-ai", {"default_provider": ""})
            assert response.status_code == 422
            assert response.headers["content-type"] == "application/json"
            assert response.json()["detail"][0]["type"] == "missing"
            assert response.json()["detail"][0]["loc"] == ["body", "default_provider"]
            auth.assert_not_called()
            save.assert_not_called()

        # A stored legacy protocol must survive an absent update, while an
        # explicit empty field resets it to native in the persisted row.
        with Session(engine) as session:
            vendor = Vendor(name="Example", protocol="legacy")
            session.add(vendor)
            session.commit()
            session.refresh(vendor)
            vid = vendor.id
        with (
            patch.object(vendors, "_flag_off_block", return_value=None),
            patch.object(vendors, "_admin_block", return_value=(None, None)),
            patch.object(vendors, "_vendor_or_404", return_value=(None, None)),
        ):
            assert post(f"/vendors/{vid}", {"name": "Example"}).status_code == 303
            with Session(engine) as session:
                assert session.get(Vendor, vid).protocol == "legacy"
            assert post(f"/vendors/{vid}", {"name": "Example", "protocol": ""}).status_code == 303
            with Session(engine) as session:
                assert session.get(Vendor, vid).protocol == "native"


def test_scalar_file_refusals():
    fixture = FastAPI()
    fixture.add_exception_handler(StarletteHTTPException, _http_error)
    for router in (workflows.router, admin.router, vendors.router):
        fixture.include_router(router)
    client = TestClient(fixture)
    cases = (
        (
            "/workflows/1/steps/2/move",
            "/workflows/{workflow_id}/steps/{step_id}/move",
            "parallel_group",
            {"position": "0"},
        ),
        ("/settings", "/settings", "prompt_tap_envelopes_enabled", {"org_name": "Example"}),
        ("/vendors/1", "/vendors/{vid}", "protocol", {"name": "Example"}),
    )
    failures = []
    for path, schema_path, field, data in cases:
        # The public schema still advertises scalar text, never an upload field.
        body = fixture.openapi()["paths"][schema_path]["post"]
        ref = body["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]["$ref"]
        prop = fixture.openapi()["components"]["schemas"][ref.rsplit("/", 1)[1]]["properties"][
            field
        ]
        assert prop["anyOf"] == [{"type": "string"}, {"type": "null"}]
        response = client.post(
            path,
            data=data,
            files={field: ("probe.txt", b"lane", "text/plain")},
            follow_redirects=False,
        )
        if not (
            response.status_code == 400
            and response.headers["content-type"].startswith("text/html")
            and "<html" in response.text.lower()
            and f"{field} must be a text form field" in response.text
        ):
            failures.append(f"{field}: {response.status_code} {response.headers['content-type']}")
    assert not failures, failures


def test_starlette_form_parser_limits():
    fixture = FastAPI()

    @fixture.post("/limits")
    async def limits(request: Request):
        form = await request.form(max_fields=2, max_part_size=4)
        return {"items": [(key, str(value)) for key, value in form.multi_items()]}

    client = TestClient(fixture, raise_server_exceptions=False)
    cases = (
        (
            "urlencoded field count",
            {
                "content": "a=1&b=2&c=3",
                "headers": {"content-type": "application/x-www-form-urlencoded"},
            },
            "Too many fields",
        ),
        (
            "urlencoded part size",
            {
                "content": "a=12345",
                "headers": {"content-type": "application/x-www-form-urlencoded"},
            },
            "Field exceeded maximum size",
        ),
        (
            "multipart field count",
            {"files": [("a", (None, "1")), ("b", (None, "2")), ("c", (None, "3"))]},
            "Too many fields",
        ),
        (
            "multipart part size",
            {"files": {"a": (None, "12345")}},
            "Part exceeded maximum size",
        ),
    )
    for label, kwargs, detail in cases:
        response = client.post("/limits", **kwargs)
        assert response.status_code == 400, (label, response.status_code, response.text)
        assert detail in response.json()["detail"], (label, response.json())

    accepted = client.post(
        "/limits",
        content="a=1&b=2",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert accepted.status_code == 200 and accepted.json()["items"] == [["a", "1"], ["b", "2"]]


def test_starlette_fileresponse_contract():
    fixture = FastAPI()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "artifact.unknown-extension"
        path.write_bytes(b"0123456789")

        @fixture.get("/artifact")
        def artifact():
            return FileResponse(path)

        client = TestClient(fixture)
        full = client.get("/artifact")
        assert full.status_code == 200 and full.content == b"0123456789"
        assert full.headers["content-type"] == "application/octet-stream"
        assert full.headers["accept-ranges"] == "bytes"
        partial = client.get("/artifact", headers={"Range": "bytes=2-5"})
        assert partial.status_code == 206 and partial.content == b"2345"
        assert partial.headers["content-range"] == "bytes 2-5/10"


if __name__ == "__main__":
    init_db()
    test_starlette_1x_template_contract()
    test_routes()
    test_forms()
    test_scalar_file_refusals()
    test_starlette_form_parser_limits()
    test_starlette_fileresponse_contract()
    print("FASTAPI ADOPTION OK")
