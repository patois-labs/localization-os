"""Enforced script CSP regression guard for #259, offline and throwaway data only."""

import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
from tests._bootstrap import init_env

init_env("loctms_script_csp_")
os.environ.pop("LOC_TMS_CSP_MODE", None)

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.testclient import TestClient

from app import config
from app.db import init_db
from app.main import app
from app.web import images
from app.web.security_headers import SecurityHeadersMiddleware, allow_form_action
from tests._csrf import csrf_for

_HEADER = "content-security-policy"


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.problems = []
        self.nonces = []
        self.external_script = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.external_script = "src" in attrs
        if tag == "script" and "src" not in attrs:
            self.nonces.append(attrs.get("nonce"))
            if not attrs.get("nonce"):
                self.problems.append("inline script without nonce")
        for name, value in attrs.items():
            if re.fullmatch(r"on[a-z]+", name):
                self.problems.append("inline event handler")
            if value and re.match(r"\s*javascript\s*:", value, re.I):
                self.problems.append("javascript URL")

    def handle_data(self, data):
        if self.external_script and data.strip():
            self.problems.append("inline code ignored inside external script tag")

    def handle_endtag(self, tag):
        if tag == "script":
            self.external_script = False


def markup(source):
    parser = Markup()
    parser.feed(source)
    return parser


def check_static():
    for sample in (
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        '<a href="javascript:alert(1)">bad</a>',
        '<script src="/static/test.js">ignored()</script>',
    ):
        assert markup(sample).problems
    paths = sorted((_ROOT / "app/templates").rglob("*.html"))
    paths += sorted((_ROOT / "app/static").rglob("*.js"))
    for path in paths:
        source = path.read_text(encoding="utf-8")
        source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
        # JS comments can show deliberately hostile markup as documentation.
        if path.suffix == ".js":
            source = re.sub(r"(?m)^\s*//.*$", "", source)
            source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        found = markup(source)
        assert not found.problems, (str(path.relative_to(_ROOT)), found.problems)
        if path.suffix == ".html":
            assert all(n == "{{ csp_nonce() }}" for n in found.nonces), path
    print(f"  static ratchet: {len(paths)} template/static files")


def check_html(response):
    assert response.headers["content-type"].startswith("text/html"), response.url
    policy = response.headers.get(_HEADER, "")
    match = re.search(r"script-src 'self' 'nonce-([A-Za-z0-9_-]{22})';", policy)
    assert match, (str(response.url), policy)
    for directive in ("object-src 'none';", "base-uri 'none';", "form-action 'self'"):
        assert directive in policy, policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    parsed = markup(response.text)
    assert not parsed.problems, parsed.problems
    assert all(n == match[1] for n in parsed.nonces), (response.url, parsed.nonces)
    assert len(response.headers.get_list(_HEADER)) == 1
    return match[1]


def check_pages():
    init_db()

    async def boom(request):
        raise RuntimeError("CSP error page probe")

    app.add_route("/__csp_error", boom)
    client = TestClient(app, raise_server_exceptions=False)
    csrf_for(client)
    created = client.post(
        "/projects",
        data={"name": "CSP project", "source_locale": "en-US", "target_locales": "es-ES"},
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text
    paths = [
        ("/auth/login", 200),
        ("/", 200),
        ("/help", 200),
        (created.headers["location"], 200),
        ("/__csp_error", 500),
        ("/no-such-csp-page", 404),
        ("/security", 200),
    ]
    nonces = set()
    for path, status in paths:
        for _ in range(2):
            response = client.get(path)
            assert response.status_code == status, (path, response.status_code)
            nonce = check_html(response)
            assert nonce not in nonces, (path, nonce)
            nonces.add(nonce)
    assert "enforce</strong>" in client.get("/security").text
    for path in ("/account/layouts/project", "/openapi.json"):
        response = client.get(path)
        assert response.headers["content-type"].startswith("application/json")
        assert _HEADER not in response.headers
    print("  login/dashboard/help/project/security/500/404: matching fresh nonces; JSON unchanged")

    from sqlmodel import Session

    from app.db import engine
    from app.kernel import settings
    from app.models import Document
    from app.ports import blobstore

    settings.set_many({"image_localization_enabled": "1"})
    with Session(engine) as session:
        doc = Document(
            project_id=int(created.headers["location"].rsplit("/", 1)[-1]),
            filename="csp.svg",
            fmt="svg",
            status="new",
            created_by="system",
        )
        session.add(doc)
        session.commit()
        session.refresh(doc)
        doc_id = doc.id
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    blobstore.get().put(f"{doc_id}.svg", svg)
    response = client.get(f"/images/{doc_id}/original")
    assert response.status_code == 200 and response.content == svg
    assert response.headers[_HEADER] == images._serve_image(svg, "svg").headers[_HEADER]
    assert response.headers["content-disposition"].startswith("attachment;")
    print("  real SVG original-download route preserves its sandbox CSP")

    from app.services import vendor_studio_sso
    from app.web import connectors_common

    with (
        patch.object(vendor_studio_sso, "is_configured", return_value=True),
        patch.object(vendor_studio_sso, "studio_url", return_value="https://studio.example/team"),
        patch.object(vendor_studio_sso, "mint_token", return_value="fixture-handoff"),
    ):
        response = client.post("/vendor-studio/open")
        assert response.status_code == 200, response.text
        check_html(response)
        assert "form-action 'self' https://studio.example;" in response.headers[_HEADER]
        assert 'action="https://studio.example/team/sso"' in response.text
    for invalid_url in ("https://caf\u00e9.example/team", "https://studio.example:bad/team"):
        with (
            patch.dict(os.environ, {"VENDOR_STUDIO_URL": invalid_url}),
            patch.object(vendor_studio_sso, "sso_secret", return_value="fixture-handoff"),
        ):
            assert vendor_studio_sso.is_configured()
            response = client.post("/vendor-studio/open")
            assert response.status_code == 404, (invalid_url, response.status_code)
            assert "Vendor Studio is not configured." in response.text
            check_html(response)
            assert "form-action 'self';" in response.headers[_HEADER]
    print("  rejected Vendor Studio origins return the not-configured page")
    with patch.object(connectors_common.conn_oauth, "provider_app_configured", return_value=True):
        for kind, origin in (
            ("airtable", "https://airtable.com"),
            ("jira", "https://auth.atlassian.com"),
        ):
            response = client.get("/connectors/" + kind)
            check_html(response)
            assert f"form-action 'self' {origin};" in response.headers[_HEADER]
    assert "https://studio.example" not in client.get("/help").headers[_HEADER]
    print("  Vendor Studio raw HTML nonce + exact configured origin; OAuth form redirect origins")


def mini(mode="enforce", framing=""):
    inner = FastAPI()

    @inner.get("/")
    def html(request: Request):
        return HTMLResponse(f'<script nonce="{request.state.csp_nonce}">window.ok=1;</script>')

    @inner.get("/json")
    def json():
        return JSONResponse({"ok": True})

    @inner.get("/download")
    def download():
        return Response(b"test", media_type="application/octet-stream")

    @inner.get("/svg")
    def svg():
        return images._serve_image(b'<svg xmlns="http://www.w3.org/2000/svg"/>', "svg")

    @inner.get("/html-download")
    def html_download():
        return HTMLResponse(
            "export", headers={"Content-Disposition": "attachment; filename=x.html"}
        )

    @inner.get("/own")
    def own():
        return HTMLResponse("own", headers={_HEADER: "default-src 'none'"})

    return TestClient(SecurityHeadersMiddleware(inner, csp_mode=mode, frame_ancestors=framing))


def check_modes():
    assert config.CSP_MODE == "enforce"
    for raw_mode in ("", "   ", "enforce", "report-only", "off"):
        mode = raw_mode.strip() or "enforce"
        env = {**os.environ, "LOC_TMS_CSP_MODE": raw_mode}
        boot = subprocess.run(
            [sys.executable, "-c", "from app import config; print(config.CSP_MODE)"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert boot.stdout.strip() == mode, boot.stdout
        assert config._validate_csp_mode(raw_mode) == mode
        with patch.object(config, "CSP_MODE", mode):
            assert SecurityHeadersMiddleware(None)._csp_mode == mode
        for framing in ("", "https://frame.example"):
            client = mini(mode, framing)
            response = client.get("/")
            script_header = _HEADER + ("-report-only" if mode == "report-only" else "")
            if mode != "off":
                assert "script-src" in response.headers[script_header]
                nonce = markup(response.text).nonces[0]
                assert f"'nonce-{nonce}'" in response.headers[script_header]
            else:
                assert "script-src" not in response.headers.get(_HEADER, "")
            if mode != "report-only":
                assert _HEADER + "-report-only" not in response.headers
            if framing:
                assert "frame-ancestors https://frame.example" in response.headers[_HEADER]
            for path in ("/json", "/download", "/html-download", "/svg", "/own"):
                response = client.get(path)
                policy = response.headers.get(_HEADER, "")
                assert "script-src" not in policy, (path, policy)
                assert _HEADER + "-report-only" not in response.headers
                if path == "/svg":
                    expected = images._serve_image(b"<svg/>", "svg").headers[_HEADER]
                    assert policy == expected + (
                        "; frame-ancestors https://frame.example" if framing else ""
                    )
                if path == "/own":
                    assert policy.startswith("default-src 'none'")
    for bad in ("disabled", "typo", "enforce; script-src *"):
        try:
            config._validate_csp_mode(bad)
        except config.BrowserSurfaceConfigError:
            pass
        else:
            raise AssertionError(bad)
    for bad in ("https://*.example", "https://a;evil", "https://a\n.example", "https://u:p@a"):
        try:
            allow_form_action(Request({"type": "http"}), bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    print("  modes, framing merge, route policy preservation, exact-origin validation")


def base_probe():
    """Opt-in negative control uses committed sources without changing the working tree."""
    source = subprocess.check_output(
        ["git", "show", "HEAD:app/web/security_headers.py"], text=True, encoding="utf-8"
    )
    namespace = {}
    exec(compile(source, "base_security_headers.py", "exec"), namespace)
    inner = FastAPI()
    inner.add_api_route("/", lambda: HTMLResponse("base"))
    response = TestClient(namespace["SecurityHeadersMiddleware"](inner)).get("/")
    check_html(response)  # Must fail: the reviewer's section D on the base middleware.


def main():
    if os.environ.get("LOC_TMS_CSP_BASE_PROBE") == "1":
        base_probe()
    check_static()
    check_pages()
    check_modes()
    print("verify_script_csp OK")


if __name__ == "__main__":
    main()
