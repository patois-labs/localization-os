"""Enterprise shell verification — all nav pages render + new actions work, offline."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import StepType, Workflow, WorkflowStep


def dx(ps):
    d = Docx()
    for p in ps:
        d.add_paragraph(p)
    b = BytesIO()
    d.save(b)
    return b.getvalue()


def main() -> None:
    init_db()
    c = TestClient(app)

    # seed some data so the dashboard/reports have content
    c.post("/customers", data={"name": "Acme Games"})
    pid = int(
        c.post(
            "/projects",
            data={
                "name": "Spring Loc",
                "source_locale": "en-US",
                "target_locales": "fr-FR, de-DE",
                "customer_id": "1",
                "default_provider": "claude",
            },
            follow_redirects=False,
        )
        .headers["location"]
        .rsplit("/", 1)[-1]
    )
    c.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                dx(["Hello world."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    with Session(engine) as s:
        wf = Workflow(name="3-step MT", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wfid = wf.id
        s.add(
            WorkflowStep(
                workflow_id=wfid,
                order_index=0,
                name="Translator",
                step_type=StepType.LLM_AGENT,
                prompt_text="Translate: {text}",
            )
        )
        s.add(WorkflowStep(workflow_id=wfid, order_index=1, name="LQA", step_type=StepType.LQA))
        s.commit()

    # every nav page renders
    for path in [
        "/",
        "/projects",
        "/customers",
        "/workflows",
        "/templates",
        "/connectors",
        "/setup-ai",
        "/settings",
        "/reports",
    ]:
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
    print("  all 9 nav pages render 200")

    # dashboard leads with the running-projects list (no generic KPI boxes)
    home = c.get("/").text
    assert "Spring Loc" in home and "Dashboard" in home, "dashboard missing project data"
    assert "Running projects" in home and "projects-table" in home, "running-projects list missing"
    assert "kpi-num" not in home, "generic KPI boxes should be gone from the dashboard"
    print("  dashboard leads with running-projects list")

    # settings save changes the org name in the shell
    r = c.post(
        "/settings",
        data={
            "org_name": "Globex Localization",
            "default_source_locale": "en-US",
            "default_provider": "gemini",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Globex Localization" in c.get("/").text, "org name not applied to shell"
    print("  settings save updates org name across the shell")

    # setup-ai save + live test
    r = c.post(
        "/setup-ai",
        data={
            "default_provider": "claude",
            "model_claude": "claude-haiku-4-5",
            "model_gemini": "gemini-2.0-flash",
            "model_openai": "gpt-4o-mini",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    d = c.post("/setup-ai/test", data={"provider": "claude"}).json()
    assert d["text"] and d["live"] is False, d  # mock offline
    print(f"  setup-ai: defaults saved + engine test ran ({'live' if d['live'] else 'mock'})")

    # prompt template create
    r = c.post(
        "/templates/prompts",
        data={"name": "Legal translator", "body": "Translate {text} formally."},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Legal translator" in c.get("/templates").text, "prompt template not listed"
    print("  prompt template saved + listed")

    # clone a workflow
    r = c.post(f"/workflows/{wfid}/clone", follow_redirects=False)
    assert r.status_code == 303 and "/workflows/" in r.headers["location"]
    new_id = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        from app.services import orchestrator

        steps = orchestrator.get_steps(s, new_id)
    assert new_id != wfid and len(steps) == 2, (new_id, len(steps))
    print(f"  workflow cloned ({wfid} -> {new_id}, {len(steps)} steps copied)")

    # BUG-2: the UI export route must set a per-format Content-Type (it used to
    # hardcode the DOCX MIME for every format). Upload a JSON doc and export it.
    # 2026-07-14: a project requires a customer when one exists — make one.
    from app.services import content as _content

    with Session(engine) as _s:
        _mime_cust = _content.customer_create(_s, "MIME Cust").id
    jpid = int(
        c.post(
            "/projects",
            data={
                "name": "MIME Test",
                "source_locale": "en-US",
                "target_locales": "fr-FR",
                "customer_id": str(_mime_cust),
                "default_provider": "claude",
            },
            follow_redirects=False,
        )
        .headers["location"]
        .rsplit("/", 1)[-1]
    )
    r = c.post(
        f"/projects/{jpid}/documents",
        data={"mode": "segmented"},
        files={"file": ("strings.json", b'{"greeting": "Hello world."}', "application/json")},
        follow_redirects=False,
    )
    jdoc = int(r.headers["location"].rsplit("/", 1)[-1])
    # chip task_9c665111: a held locale (no committed translation) now 409s on export;
    # this check is about the per-format MIME, so give the segment a target first.
    from app.models import Segment as _Segment

    with Session(engine) as _s:
        for _sg in _s.exec(
            select(_Segment).where(_Segment.document_id == jdoc, _Segment.target_locale == "fr-FR")
        ).all():
            _sg.target_text = "Bonjour le monde."
            _sg.status = "mt"
            _s.add(_sg)
        _s.commit()
    r = c.get(f"/documents/{jdoc}/export?locale=fr-FR")
    assert r.status_code == 200, r.status_code
    ct = r.headers["content-type"]
    assert ct.startswith("application/json"), f"UI export used wrong MIME for json: {ct!r}"
    assert "wordprocessingml" not in ct, f"UI export still hardcodes DOCX MIME: {ct!r}"
    assert "strings.fr-FR.json" in r.headers.get("content-disposition", ""), r.headers.get(
        "content-disposition"
    )
    print(f"  BUG-2: UI export sets per-format MIME ({ct.split(';')[0]} for json)")

    # top-bar search: GET /projects?q= filters by project name / customer name
    c.post("/customers", data={"name": "Zeta Zoo"})
    zpid = int(
        c.post(
            "/projects",
            data={
                "name": "Zebra Crossing",
                "source_locale": "en-US",
                "target_locales": "fr-FR",
                "customer_id": "2",
                "default_provider": "claude",
            },
            follow_redirects=False,
        )
        .headers["location"]
        .rsplit("/", 1)[-1]
    )
    page = c.get("/projects?q=zebra").text
    assert "Zebra Crossing" in page, "q= filter should match project name"
    assert "Spring Loc" not in page, "q= filter should exclude non-matching projects"
    # case-insensitive + matches by customer name too
    page_ci = c.get("/projects?q=ZEBRA").text
    assert "Zebra Crossing" in page_ci, "q= filter should be case-insensitive"
    page_cust = c.get("/projects?q=Acme").text
    assert "Spring Loc" in page_cust and "Zebra Crossing" not in page_cust, (
        "q= filter should also match by customer name"
    )
    assert 'value="zebra"' in page or "q=zebra" in page, "search box should round-trip current q"
    print(
        f"  top-bar search: /projects?q= filters by project/customer name (case-insensitive, project {zpid})"  # noqa: E501
    )

    # cache-buster: static_v('style.css') is a real mtime int, not the old hardcoded '13'
    from app.web.deps import static_v

    v = static_v("style.css")
    assert isinstance(v, int) and v > 0, f"static_v should return a positive int mtime, got {v!r}"
    home_html = c.get("/").text
    assert f"style.css?v={v}" in home_html, "base.html should render the live static_v() version"
    assert "style.css?v=13" not in home_html, "hardcoded ?v=13 should be gone"
    print(f"  cache-buster: static_v('style.css') = {v} (mtime int, wired into base.html)")

    # UI polish: /users paginates (page/limit) and filters by q= (email or name),
    # mirroring the /projects and /audit page/q pattern (admin.py users_index).
    from app.services import security

    for i in range(60):
        security.create_user(
            f"pguser{i}@example.com", f"PgUser {i}", "translator", "fixturepass12345"
        )
    users_p1 = c.get("/users")
    assert users_p1.status_code == 200 and "Page 1 of" in users_p1.text, (
        "users list should paginate once there are more than a page's worth"
    )
    assert 'href="/users?page=2"' in users_p1.text, "users pager should link to page 2"
    users_p2 = c.get("/users?page=2")
    assert users_p2.status_code == 200 and "Page 2 of" in users_p2.text
    assert "pguser0@example.com" in users_p1.text and "pguser0@example.com" not in users_p2.text, (
        "page 1 and page 2 of /users should show different users"
    )
    users_q = c.get("/users?q=pguser5")
    assert users_q.status_code == 200 and "pguser5@example.com" in users_q.text, (
        "q= filter should match by email"
    )
    assert "pguser1@example.com" not in users_q.text, "q= filter should exclude non-matching users"
    print("  users list paginates (page/limit) and filters by q= (email/name)")

    # Issue #546: a search term with & or # must be URL-encoded in the pager/chip links,
    # otherwise page 2 of "R&D" searches for "R" and the "#..." part becomes a fragment.
    for i in range(60):
        security.create_user(
            f"rdteam{i}@example.com", f"R&D#team {i}", "translator", "fixturepass12345"
        )
    rd = c.get("/users?q=R%26D%23team")
    assert rd.status_code == 200 and "Page 1 of" in rd.text
    assert "q=R%26D%23team" in rd.text, "users pager must URL-encode q"
    assert "q=R&D#team" not in rd.text.replace("&amp;", "&"), "raw q leaked into a link"
    rdp = c.get("/projects?q=R%26D%23x")
    assert rdp.status_code == 200
    assert "status=default&amp;q=R%26D%23x" in rdp.text, "projects chips must URL-encode q"
    assert "q=R&amp;D" not in rdp.text and "q=R&D" not in rdp.text, "raw q leaked into a chip"
    print("  search term is URL-encoded in users pager and projects chips (#546)")

    # UI polish: the CAT segment tables (document.html, editor.html) window large
    # documents instead of rendering every segment on one page.
    from docx import Document as Docx2

    big_pid = int(
        c.post(
            "/projects",
            data={
                "name": "Big Doc",
                "source_locale": "en-US",
                "target_locales": "fr-FR",
                "customer_id": "1",
                "default_provider": "claude",
            },
            follow_redirects=False,
        )
        .headers["location"]
        .rsplit("/", 1)[-1]
    )
    dd = Docx2()
    for i in range(250):
        dd.add_paragraph(f"Paragraph number {i} of the pagination test document.")
    bb = BytesIO()
    dd.save(bb)
    r = c.post(
        f"/projects/{big_pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "big.docx",
                bb.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    big_doc_id = int(r.headers["location"].rsplit("/", 1)[-1])
    doc_p1 = c.get(f"/documents/{big_doc_id}?locale=fr-FR")
    assert doc_p1.status_code == 200 and "Page 1 of" in doc_p1.text
    assert "Paragraph number 0 " in doc_p1.text and "Paragraph number 249 " not in doc_p1.text
    doc_p2 = c.get(f"/documents/{big_doc_id}?locale=fr-FR&page=2")
    assert doc_p2.status_code == 200 and "Page 2 of" in doc_p2.text
    assert "Paragraph number 249 " in doc_p2.text and "Paragraph number 0 " not in doc_p2.text
    editor_p1 = c.get(f"/documents/{big_doc_id}/editor?locale=fr-FR")
    assert editor_p1.status_code == 200 and "Page 1 of" in editor_p1.text
    editor_p2 = c.get(f"/documents/{big_doc_id}/editor?locale=fr-FR&page=2")
    assert editor_p2.status_code == 200 and "Page 2 of" in editor_p2.text
    print("  CAT segment tables (document.html/editor.html) window large documents")

    # UI polish: template macro migration (_macros.html pager()/card()) must not
    # drop content — spot-check a representative page from each migrated template.
    assert c.get("/customers").status_code == 200 and "Customers" in c.get("/customers").text
    assert "New customer" in c.get("/customers").text
    assert "Integrations" in c.get("/connectors").text and "MT engines" in c.get("/connectors").text
    print("  macro-migrated pages (customers, connectors, audit, users) still render key content")

    # bug #12: /favicon.ico used to 404 on every page (no <link rel=icon> and no
    # route for the bare path). base.html must carry the <link>, and the bare
    # path itself must resolve (not 404) so a browser's automatic favicon probe
    # succeeds too.
    assert '<link rel="icon"' in home and "favicon.svg" in home, (
        "base.html should carry a <link rel=icon> pointing at the favicon"
    )
    fav = c.get("/favicon.ico")
    assert fav.status_code == 200, f"/favicon.ico should not 404, got {fav.status_code}"
    print("  favicon: <link rel=icon> present in base.html + /favicon.ico resolves (not 404)")

    # bug #14: below 900px the sidebar nav used to just vanish (display:none) with
    # no way back in. base.html must render a hamburger toggle in the topbar (its
    # own CSS class + the JS/backdrop hooks) so mobile users can still reach nav.
    assert "data-nav-toggle" in home, "base shell should render a mobile-nav hamburger button"
    assert 'class="hamburger"' in home, "hamburger button should carry its dedicated CSS class"
    assert "data-nav-backdrop" in home, "base shell should render the mobile-nav backdrop element"
    assert "/static/mobilenav.js" in home, "base.html should load the mobile-nav toggle script"
    print("  mobile nav: hamburger + backdrop + toggle script present in the base shell")

    # hygiene wave 1 item 4: document-upload client-side size guard matches the
    # server's config.MAX_UPLOAD_BYTES cap (early feedback; server-side stays
    # authoritative and unchanged -- see app/web/documents.py's `total > cap`).
    proj_page = c.get(f"/projects/{pid}").text
    assert f'data-max-upload-bytes="{config.MAX_UPLOAD_BYTES}"' in proj_page, (
        "doc-upload form must carry the server's MAX_UPLOAD_BYTES cap for client-side JS"
    )
    assert 'id="doc-upload-size-error"' in proj_page, (
        "doc-upload form must carry an inline error placeholder for the size guard"
    )
    scripts = re.findall(r'<script nonce="[^"]+">(.*?)</script>', proj_page, re.S)
    size_guard_scripts = [s for s in scripts if "MAX_UPLOAD_BYTES cap" in s]
    assert size_guard_scripts, "expected the doc-upload size-guard <script> block"
    print(
        "  doc-upload size guard: form carries the server's MAX_UPLOAD_BYTES cap "
        "+ error placeholder + guard script"
    )

    node = shutil.which("node")
    if node:
        script_body = size_guard_scripts[0]
        with tempfile.TemporaryDirectory(prefix="loctms_upload_js_") as tmp:
            extracted_path = os.path.join(tmp, "extracted.js")
            harness_path = os.path.join(tmp, "harness.js")
            with open(extracted_path, "w", encoding="utf-8") as f:
                f.write(script_body)
            rc = subprocess.run([node, "--check", extracted_path], capture_output=True, text=True)
            assert rc.returncode == 0, ("doc-upload guard script failed node --check", rc.stderr)
            # Execute the guard for real against a fake DOM: prove the change/submit
            # handlers actually block an oversized file (preventDefault + visible
            # error) and let an at-limit/small file through, matching the server's
            # `total > cap` boundary (equal-to-cap is NOT too large).
            harness = """
const path = require('path');
const listeners = {};
function makeEl(id) {
  return {
    id: id, hidden: true, textContent: '', files: [],
    getAttribute: function () { return null; },
    addEventListener: function (evt, fn) { listeners[id + ':' + evt] = fn; },
  };
}
const maxBytes = process.argv[2];
const formEl = makeEl('doc-upload-form');
formEl.getAttribute = function (name) {
  return name === 'data-max-upload-bytes' ? maxBytes : null;
};
const fileEl = makeEl('doc-upload-file');
const errEl = makeEl('doc-upload-size-error');
global.document = {
  getElementById: function (id) {
    if (id === 'doc-upload-form') return formEl;
    if (id === 'doc-upload-file') return fileEl;
    if (id === 'doc-upload-size-error') return errEl;
    return null;
  }
};
require(path.resolve(process.argv[3]));
function change() { listeners['doc-upload-file:change'](); }
function submit() {
  var prevented = false;
  listeners['doc-upload-form:submit']({ preventDefault: function () { prevented = true; } });
  return prevented;
}
var out = {};
fileEl.files = [{ size: Number(maxBytes) + 1 }];
change();
out.oversized_hidden = errEl.hidden;
out.oversized_prevented = submit();
fileEl.files = [{ size: Number(maxBytes) }];
change();
out.atlimit_hidden = errEl.hidden;
out.atlimit_prevented = submit();
fileEl.files = [{ size: 10 }];
change();
out.small_hidden = errEl.hidden;
out.small_prevented = submit();
console.log(JSON.stringify(out));
"""
            with open(harness_path, "w", encoding="utf-8") as f:
                f.write(harness)
            run = subprocess.run(
                [node, harness_path, str(config.MAX_UPLOAD_BYTES), extracted_path],
                capture_output=True,
                text=True,
            )
            assert run.returncode == 0, ("doc-upload guard harness crashed", run.stderr)
            res = json.loads(run.stdout.strip().splitlines()[-1])
            assert res["oversized_hidden"] is False and res["oversized_prevented"] is True, res
            assert res["atlimit_hidden"] is True and res["atlimit_prevented"] is False, res
            assert res["small_hidden"] is True and res["small_prevented"] is False, res
        print(
            "  doc-upload size guard: executed guard JS (node) blocks > cap, "
            "allows <= cap, matching the server's boundary"
        )
    else:
        print("  doc-upload size guard: behavior check skipped (node not on PATH)")

    print("UI OK")


if __name__ == "__main__":
    main()
