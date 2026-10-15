"""API auth precedes required input validation; hermetic and offline."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_auth_order_")

from app.main import app
from app.web._routes import walk_routes

# Explicit callable identities only: a DB/session dependency is not authentication.
# Deliberate hook for a future route-level auth dependency.
# Empty on purpose: the exemption is unspoofable while empty.
AUTH_DEPENDENCIES = frozenset()
ALLOWLIST = set()


def walk_dependants(dependant):
    yield dependant
    for child in dependant.dependencies:
        yield from walk_dependants(child)


def check_routes():
    problems = []
    for route in walk_routes(app):
        if not getattr(route, "path", "").startswith("/api/"):
            continue
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        required = sorted(
            {
                p.alias
                for node in walk_dependants(dependant)
                for p in node.header_params
                + node.cookie_params
                + node.query_params
                + node.body_params
                if p.field_info.is_required()
            }
        )
        authenticated = any(d.call in AUTH_DEPENDENCIES for d in dependant.dependencies)
        for method in sorted(route.methods):
            if required and not authenticated and (method, route.path) not in ALLOWLIST:
                problems.append(f"RATCHET {method} {route.path}: required {', '.join(required)}")
    return problems


def check_requests():
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.models import Project, Role, User
    from app.services import apikeys, settings

    init_db()
    settings.set_many({"image_localization_enabled": "1"})
    # Same runtime-issued admin key fixture as verify_export_held_locale.
    with Session(engine) as session:
        user = User(email="auth-order@x.io", name="A", role=Role.ADMIN, is_active=True)
        session.add(user)
        session.commit()
        session.refresh(user)
        uid = user.id
    _, raw = apikeys.create_key(uid, "auth-order", scopes="api docs")
    headers = {"X-Loc-Token": raw}
    cases = [
        ("GET", "/api/documents/1/download", "locale", {"params": {"locale": "fr-FR"}}),
        ("GET", "/api/documents/1/paragraphs", "locale", {"params": {"locale": "fr-FR"}}),
        ("GET", "/api/images/1/export", "locale", {"params": {"locale": "fr-FR"}}),
        (
            "GET",
            "/api/projects/1/tm/export",
            "source",
            {"params": {"source": "en-US", "target": "fr-FR"}},
        ),
        (
            "POST",
            "/api/images",
            "project_id",
            {
                "data": {"project_id": "1"},
                "files": {"file": ("test.svg", b"<svg/>", "image/svg+xml")},
            },
        ),
    ]
    problems = []
    client = TestClient(app)

    def expect(label, method, path, status, body=None, **kwargs):
        response = client.request(method, path, **kwargs)
        print(f"{label} {method} {path}: {response.status_code} {response.text}")
        if response.status_code != status or (body is not None and response.json() != body):
            problems.append(f"FAIL {label} {method} {path}: expected {status} {body}")

    for method, path, missing, present in cases:
        for _ in range(2):
            expect("anonymous", method, path, 401, {"ok": False, "error": "unauthorized"})
        expect(
            "missing",
            method,
            path,
            400,
            {"ok": False, "error": f"{missing} is required"},
            headers=headers,
        )
        expect("present (absent object)", method, path, 404, headers=headers, **present)
        for blank in ("", "   "):
            values = dict(present.get("params", present.get("data", {})))
            values[missing] = blank
            inputs = {"data" if method == "POST" else "params": values}
            expect(
                "blank",
                method,
                path,
                400,
                {"ok": False, "error": f"{missing} is required"},
                headers=headers,
                **inputs,
            )
    for target in (None, "", "   "):
        params = {"source": "en-US"}
        if target is not None:
            params["target"] = target
        expect(
            "missing/blank target",
            "GET",
            "/api/projects/1/tm/export",
            400,
            {"ok": False, "error": "target is required"},
            headers=headers,
            params=params,
        )
    expect(
        "missing file",
        "POST",
        "/api/images",
        400,
        {"ok": False, "error": "file is required"},
        headers=headers,
        data={"project_id": "1"},
    )
    for invalid in ("abc", "1.5"):
        expect(
            "invalid project",
            "POST",
            "/api/images",
            400,
            {"ok": False, "error": "project_id must be an integer"},
            headers=headers,
            data={"project_id": invalid},
        )
        expect(
            "anonymous invalid project",
            "POST",
            "/api/images",
            401,
            {"ok": False, "error": "unauthorized"},
            data={"project_id": invalid},
        )
    with Session(engine) as session:
        project = Project(name="Auth order upload", source_locale="en-US")
        session.add(project)
        session.commit()
        session.refresh(project)
        pid = str(project.id)
    import io

    from PIL import Image

    png = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(png, format="PNG")
    os.environ["LOC_TMS_IMAGE_DETECTOR"] = "mock"
    unicode_pid = "".join(chr(ord(c) + 0xFEE0) for c in pid)
    for invalid in (unicode_pid, "+" + pid, "-" + pid, " " + pid + " ", "9" * 40, "9" * 4400):
        expect(
            "invalid upload project " + ascii(invalid[:45]),
            "POST",
            "/api/images",
            400,
            {"ok": False, "error": "project_id must be an integer"},
            headers=headers,
            data={"project_id": invalid},
            files={"file": ("test.png", png.getvalue(), "image/png")},
        )
    expect(
        "valid upload project",
        "POST",
        "/api/images",
        200,
        headers=headers,
        data={"project_id": pid},
        files={"file": ("test.png", png.getvalue(), "image/png")},
    )
    _, wrong_scope = apikeys.create_key(uid, "auth-order-wrong-scope", scopes="keys")
    for method, path, _missing, _present in cases:
        expect("wrong scope", method, path, 403, headers={"X-Loc-Token": wrong_scope})
    settings.set_many({"image_localization_enabled": "0"})
    for method, path in (("GET", "/api/images/1/export"), ("POST", "/api/images")):
        expect("feature disabled", method, path, 404)
        expect("feature disabled authed", method, path, 404, headers=headers)
    return problems


def check_help():
    text = Path("app/templates/help.html").read_text(encoding="utf-8")
    problems = []
    for route, param in (
        ("/api/documents/{doc_id}/download", "locale"),
        ("/api/documents/{doc_id}/paragraphs", "locale"),
        ("/api/images/{doc_id}/export", "locale"),
        ("/api/projects/{pid}/tm/export", "source or target"),
    ):
        sentence = f"401 without a token; 400 when {param} is missing or blank."
        row = next(line for line in text.splitlines() if f"<code>{route}</code>" in line)
        if sentence not in row:
            problems.append(f"FAIL help {route}: missing {sentence}")
    return problems


def check_ratchet_coverage():
    from fastapi import Body, Cookie, Depends, FastAPI, Header, Query

    probe = FastAPI()

    def leaf(cookie=Cookie(...), query=Query(...), body=Body(...)):
        pass

    def middle(value=Depends(leaf)):
        pass

    @probe.post("/api/ratchet-probe")
    def handler(header=Header(...), value=Depends(middle)):
        pass

    with patch(__name__ + ".app", probe):
        actual = check_routes()
    expected = ["RATCHET POST /api/ratchet-probe: required body, cookie, header, query"]
    if actual != expected:
        return [f"FAIL recursive ratchet: expected {expected!r}, got {actual!r}"]
    return []


def main():
    problems = check_routes()
    for problem in problems:
        print(problem)
    if "--ratchet-only" not in sys.argv:
        problems.extend(check_help())
        problems.extend(check_ratchet_coverage())
        problems.extend(check_requests())
    if problems:
        for problem in problems:
            print(problem)
        return 1
    print("API AUTH ORDER OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
