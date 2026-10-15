"""SPEC-key-based-projects section 10.4 — THE SURFACES (Batch 4, Wave B).

Wave A proved the engine. This suite proves the three surfaces over it behave, and
it is written AUTH-ON wherever the claim is about authorization, because the Ask+Draft
review taught this project exactly one lesson twice: an auth-OFF suite against a
service seam leaves web-route scope-stripping green.

Groups:
  [A] the web key grid: create / edit base + value / metadata / plural / delete, auth-ON
      as an admin, and READ-ONLY as a viewer (whose POSTs really are refused).
  [B] the create form: the type selector is gated on the kill switch, a key project
      needs >=1 target locale, and a document project's create path is unchanged.
  [C] the CLI group: every verb local, every verb over --remote through the real /api,
      and the --cursor-file round trip.
  [D] the kill switch OFF on ALL THREE surfaces: web 404, /api 404, CLI refusal, and
      the type selector + grid affordances gone from the HTML.
  [E] ASCII-safe CLI output on the whole key group.
  [F] the two WAVE B OBLIGATIONS the spec wrote down: the run launcher wraps
      `keys.actor_scope` (so the raw run thread it starts is attributable even though
      the route runs on anyio's POOL), and a DELETED key's segments are out of run
      scope.
  [G] internal key documents are invisible on every document surface, and a direct
      fetch of one through the document routes is a 404 -- including the four RUN
      launchers, the CAT-editor carve-out (which stops at the TRASH document) and the
      four doc-addressed /api routes a plain `docs` token can reach.
  [H] the /api write gate: a VIEWER's keys-scoped token READS the group but is 403'd
      on every write. A correct scope is not a role -- the token is scoped exactly
      right; its owner just may not write.
  [I] the two LIFECYCLE seams that reached internal key documents anyway: a document
      cannot be uploaded into a key project, and a retention sweep never ages one out.
  [J] the fix round's CLI / parity / audit cells: a cursor file belonging to another
      project, `keys add --remote` vs a duplicate, the `restore` + `edit` verbs on both
      arms, an all-or-nothing PATCH, the audit trail, and restore filling a locale
      added while the key was tombstoned.
  [K] the fix round's small-surface cells: keys-appropriate progress, the project-export
      message, a viewer's Export card, one flag name + a refused limit, the echoed
      delta limit, and what the kill switch leaves behind.
  [L] the live-QA findings' surface half: an export naming what the tag guard did on
      all four surfaces (K2), and a singular `--base` on a plural key (K5).
  [M] the FIX ROUND over [L]: the FIRST base edit re-fanning that singular string on
      both surfaces (F6), a web import showing counts but never its per-key notes
      (F4), and the transport download's kept-damaged headers -- which no test at the
      HTTP or `--remote` level asserted at all.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). Final line:
"KEYS SURFACES OK".
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stdout
from urllib.parse import unquote

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

_TMP = init_env("loctms_keysurf_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli import _remote
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Customer,
    Document,
    Project,
    ProjectMember,
    ProjectType,
    Role,
    Segment,
    SegStatus,
    StringKey,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    apikeys,
    content,
    key_files,
    security,
    settings,
)
from app.services import (
    keys as keys_svc,
)
from app.web import keys as keysweb
from tests._csrf import csrf_for

PW = "keysurfaces-pw-123456"
_N = [0]


def _uniq() -> int:
    _N[0] += 1
    return _N[0]


# --- harness ------------------------------------------------------------------
def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _run_json(argv):
    rc, out = _run(["--json"] + argv)  # noqa: RUF005
    return rc, (_json.loads(out) if out.strip() else {})


def _run_both(argv):
    """`_run` with STDERR folded in — a CliError prints its `error: ...` line there,
    so a refusal probe that only reads stdout can only ever assert the exit code."""
    from contextlib import redirect_stderr

    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _auth(on: bool) -> None:
    settings.set_many({"security_auth_required": "1" if on else "0"})
    settings.invalidate_cache()


def _switch(on: bool) -> None:
    settings.set_many({"key_projects_enabled": "1" if on else "0"})
    settings.invalidate_cache()


def _login(email, role=Role.ADMIN, customer_id=None):
    security.create_user(email, email, role, PW, customer_id=customer_id)
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code)
    csrf_for(c)
    return c


def _key_for(email, scopes="api keys"):
    user = security.create_user(email, email, Role.ADMIN, PW)
    _row, raw = apikeys.create_key(user.id, "surf", scopes=scopes)
    return raw


def _mock_transport(client):
    """httpx.MockTransport forwarding the CLI's --remote arm to the in-process
    TestClient, so `--remote` really crosses the HTTP boundary with no network."""
    import httpx

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = client.request(
            request.method, path, headers=dict(request.headers), content=request.content
        )
        # content-type plus the EXPORT REPORT headers: they are the only channel a
        # raw-bytes download has, and the --remote CLI arm rebuilds its substitution
        # report from them (live-QA K2). Dropping them here would make the remote arm
        # look silent for a reason that exists only in this harness.
        fwd = {"content-type": r.headers.get("content-type", "")}
        for name in (
            key_files.HDR_SUBSTITUTED,
            key_files.HDR_SUBSTITUTED_DETAIL,
            key_files.HDR_SUBSTITUTED_KEYS,
            key_files.HDR_KEPT,
            key_files.HDR_KEPT_KEYS,
            key_files.HDR_WITHHELD,
            key_files.HDR_WITHHELD_KEYS,
        ):
            if r.headers.get(name):
                fwd[name] = r.headers[name]
        return httpx.Response(r.status_code, content=r.content, headers=fwd)

    return httpx.MockTransport(handler)


def _mk_customer(name=None):
    with Session(engine) as s:
        c = Customer(name=name or "SurfCust %d" % _uniq())  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_key_project(cust_id, name=None, targets="de-DE, ru-RU"):
    with Session(engine) as s:
        p = content.project_create(
            s,
            name or "SurfKeys %d" % _uniq(),  # noqa: UP031
            source_locale="en-US",
            target_locales=targets,
            customer_id=str(cust_id),
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _mk_doc_project(cust_id, name=None):
    with Session(engine) as s:
        p = content.project_create(
            s,
            name or "SurfDocs %d" % _uniq(),  # noqa: UP031
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=str(cust_id),
        )
        return p.id


def _add_key(pid, name, base="Hello", namespace="", plural=False):
    with Session(engine) as s:
        project = s.get(Project, pid)
        return keys_svc.create_key(
            s,
            project,
            name,
            namespace=namespace,
            is_plural=plural,
            base_value=({"other": base} if plural else base),
            actor="fixture",
        ).id


def _view(key_id):
    with Session(engine) as s:
        return keys_svc.key_view(s, s.get(StringKey, key_id))


# ============================================================================
# [A] the web key grid, AUTH-ON
# ============================================================================
def test_web_grid_crud_auth_on():
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust)
        admin = _login("keysurf-admin@x.com")

        r = admin.get("/projects/%d/keys" % pid)  # noqa: UP031
        assert r.status_code == 200, r.status_code
        assert "de-DE" in r.text and "ru-RU" in r.text, "the grid must show the locales"

        # create
        r = admin.post(
            "/projects/%d/keys/add" % pid,  # noqa: UP031
            data={
                "name": "error::generic",
                "base_value": "Boom.",
                "description": "the generic error",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        with Session(engine) as s:
            key = keys_svc.get_key(s, pid, "error::generic")
            assert key is not None, "the grid create must land"
            kid = key.id
        assert _view(kid)["base"] == {"": "Boom."}, _view(kid)

        # a value edit
        r = admin.post(
            "/projects/%d/keys/%d/value" % (pid, kid),  # noqa: UP031
            data={"locale": "de-DE", "text": "Bumm."},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert _view(kid)["values"]["de-DE"][0]["text"] == "Bumm."

        # a BASE edit auto-unverifies the locale that already had a translation
        r = admin.post(
            "/projects/%d/keys/%d/base" % (pid, kid),  # noqa: UP031
            data={"text": "Boom, again."},
            follow_redirects=False,
        )
        assert r.status_code == 303
        de = _view(kid)["values"]["de-DE"][0]
        assert de["status"] == "needs_review", de
        assert any(f.get("type") == "source_changed" for f in de["qa_flags"]), de

        # metadata + plural drawer
        admin.post(
            "/projects/%d/keys/%d/meta" % (pid, kid),  # noqa: UP031
            data={"description": "edited", "tags": "ui, error", "char_limit": "40"},
            follow_redirects=False,
        )
        v = _view(kid)
        assert v["description"] == "edited" and v["char_limit"] == 40, v
        assert v["tags"] == ["ui", "error"], v
        admin.post(
            "/projects/%d/keys/%d/plural" % (pid, kid),  # noqa: UP031
            data={"is_plural": "1"},
            follow_redirects=False,
        )
        v = _view(kid)
        assert v["is_plural"], v
        # The promote materialized Russian's REAL CLDR set -- one/few/many plus the
        # fraction-only `other` gettext cannot express. Pinned as the full set on
        # purpose: this is the exact place a "3-form Russian" folk belief would show
        # up as a missing row.
        assert {x["plural_category"] for x in v["values"]["ru-RU"]} == {
            "one",
            "few",
            "many",
            "other",
        }, v["values"]["ru-RU"]
        assert {x["plural_category"] for x in v["values"]["de-DE"]} == {"one", "other"}, v[
            "values"
        ]["de-DE"]

        # delete needs the exact name typed
        r = admin.get("/projects/%d/keys/%d/delete" % (pid, kid))  # noqa: UP031
        assert r.status_code == 200 and "error::generic" in r.text
        admin.post(
            "/projects/%d/keys/%d/delete" % (pid, kid),  # noqa: UP031
            data={"confirm_name": "wrong"},
            follow_redirects=False,
        )
        with Session(engine) as s:
            assert s.get(StringKey, kid).deleted_at is None, (
                "a mistyped confirmation must not delete a key"
            )
        admin.post(
            "/projects/%d/keys/%d/delete" % (pid, kid),  # noqa: UP031
            data={"confirm_name": "error::generic"},
            follow_redirects=False,
        )
        with Session(engine) as s:
            assert s.get(StringKey, kid).deleted_at is not None
        # ...and the grid can SHOW it again (the delete page promises "restorable",
        # so the restore route must be reachable rather than an orphan), where it is
        # read-only and offers exactly one control.
        # (matched on the key's own route, not its NAME -- the add-key form uses
        # "error::generic" as a placeholder, which would make a name match pass
        # while the row was rendered.)
        row_marker = "/projects/%d/keys/%d/" % (pid, kid)  # noqa: UP031
        hidden = admin.get("/projects/%d/keys" % pid).text  # noqa: UP031
        assert row_marker not in hidden, "a deleted key must be out of the default grid"
        shown = admin.get("/projects/%d/keys?deleted=1" % pid).text  # noqa: UP031
        assert row_marker in shown and "Restore" in shown, (
            "the deleted filter must show the key WITH a restore control"
        )
        assert f'action="{row_marker}restore"' in shown, shown[:200]
        admin.post("/projects/%d/keys/%d/restore" % (pid, kid), follow_redirects=False)  # noqa: UP031
        with Session(engine) as s:
            assert s.get(StringKey, kid).deleted_at is None
    finally:
        _auth(False)
    print(
        "  [A] web grid: add / value / base+auto-unverify / meta / plural / "
        "delete-with-confirm / restore all land, auth-ON OK"
    )


def test_web_grid_viewer_is_read_only():
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust)
        kid = _add_key(pid, "viewer::probe", base="Original")
        viewer = _login("keysurf-viewer@x.com", role=Role.VIEWER)
        # A VIEWER is not see-all, so make it a member: the claim under test is the
        # WRITE gate, and a caller that cannot even read the page would prove it by
        # accident (and would keep passing if can_write were removed entirely).
        with Session(engine) as s:
            user = security.get_by_email("keysurf-viewer@x.com")
            s.add(ProjectMember(project_id=pid, user_id=user.id))
            s.commit()

        r = viewer.get("/projects/%d/keys" % pid)  # noqa: UP031
        assert r.status_code == 200, r.status_code
        assert "Add a key" not in r.text, (
            "a viewer must not be offered a control whose POST is refused"
        )
        assert 'name="text"' not in r.text, "a viewer must not get inline value inputs"

        for path, data in (
            ("/projects/%d/keys/add" % pid, {"name": "viewer::sneak"}),  # noqa: UP031
            ("/projects/%d/keys/%d/value" % (pid, kid), {"locale": "de-DE", "text": "nope"}),  # noqa: UP031
            ("/projects/%d/keys/%d/base" % (pid, kid), {"text": "nope"}),  # noqa: UP031
            ("/projects/%d/keys/%d/meta" % (pid, kid), {"description": "nope"}),  # noqa: UP031
            ("/projects/%d/keys/%d/plural" % (pid, kid), {"is_plural": "1"}),  # noqa: UP031
            ("/projects/%d/keys/%d/delete" % (pid, kid), {"confirm_name": "viewer::probe"}),  # noqa: UP031
            ("/projects/%d/keys/import" % pid, {}),  # noqa: UP031
            ("/projects/%d/keys/translate" % pid, {"workflow_id": "1", "locale": "de-DE"}),  # noqa: UP031
        ):
            r = viewer.post(path, data=data, follow_redirects=False)
            assert r.status_code == 403, (path, r.status_code)
        with Session(engine) as s:
            key = s.get(StringKey, kid)
            assert key.deleted_at is None and key.description == ""
            assert keys_svc.get_key(s, pid, "viewer::sneak") is None
            assert keys_svc.base_values(s, key) == {"": "Original"}
    finally:
        _auth(False)
    print(
        "  [A] a VIEWER reads the grid, is offered no write control, and every "
        "write route really answers 403 OK"
    )


def test_web_import_export_round_trip():
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust)
        admin = _login("keysurf-io@x.com")
        payload = _json.dumps({"title": "Hi", "body": {"lead": "Lead"}}).encode()

        r = admin.post(
            "/projects/%d/keys/import" % pid,  # noqa: UP031
            files={"file": ("common.json", payload, "application/json")},
            data={"locale": "", "fmt": "json"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        with Session(engine) as s:
            names = sorted(k.name for k in keys_svc.list_keys(s, pid))
            assert names == ["body::lead", "title"], names
            assert all(k.namespace == "common" for k in keys_svc.list_keys(s, pid))

        # a namespace-scoped export is the BARE build file
        r = admin.get("/projects/%d/keys/export?locale=&fmt=json&namespace=common" % pid)  # noqa: UP031
        assert r.status_code == 200, r.status_code
        body = _json.loads(r.content)
        assert "$meta" not in body, ("a build file must carry no marker", body)
        assert body["title"] == "Hi", body

        # the whole-project export is the MARKED transport file
        r = admin.get("/projects/%d/keys/export?locale=&fmt=json" % pid)  # noqa: UP031
        body = _json.loads(r.content)
        assert body.get("$meta", {}).get("loc_studio", {}).get("namespaces") == "qualified", body
        assert "common" in body, body

        # cleanup is refused without the typed confirmation, and the keys survive
        small = _json.dumps({"title": "Hi"}).encode()
        r = admin.post(
            "/projects/%d/keys/import" % pid,  # noqa: UP031
            files={"file": ("common.json", small, "application/json")},
            data={"cleanup": "1", "confirm_cleanup": ""},
            follow_redirects=False,
        )
        assert r.status_code == 303
        with Session(engine) as s:
            assert len(keys_svc.list_keys(s, pid)) == 2, (
                "an unconfirmed cleanup must delete nothing"
            )
        r = admin.post(
            "/projects/%d/keys/import" % pid,  # noqa: UP031
            files={"file": ("common.json", small, "application/json")},
            data={"cleanup": "1", "confirm_cleanup": "DELETE"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        with Session(engine) as s:
            assert [k.name for k in keys_svc.list_keys(s, pid)] == ["title"]
    finally:
        _auth(False)
    print(
        "  [A] web import (merge + confirmed cleanup) and BOTH export shapes "
        "(bare build file / marked transport file) OK"
    )


# ============================================================================
# [B] the create form
# ============================================================================
def test_create_form_type_selector():
    _auth(True)
    try:
        cust = _mk_customer()
        admin = _login("keysurf-create@x.com")
        _switch(True)
        page = admin.get("/projects").text
        assert 'name="project_type"' in page and 'value="key_based"' in page, (
            "the type selector must be offered while the switch is on"
        )

        # a key project REQUIRES at least one target locale
        r = admin.post(
            "/projects",
            data={
                "name": "NoTargets %d" % _uniq(),  # noqa: UP031
                "source_locale": "en-US",
                "customer_id": str(cust),
                "project_type": "key_based",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303 and "err=" in r.headers["location"], r.headers
        with Session(engine) as s:
            assert (
                s.exec(
                    select(Project).where(
                        Project.project_type == ProjectType.KEY_BASED,
                        Project.name.like("NoTargets%"),
                    )
                ).first()
                is None
            ), "a key project with no target locale must not be created"

        r = admin.post(
            "/projects",
            data={
                "name": "WithTargets %d" % _uniq(),  # noqa: UP031
                "source_locale": "en-US",
                "target_locales": ["de-DE"],
                "customer_id": str(cust),
                "project_type": "key_based",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        with Session(engine) as s:
            proj = s.exec(select(Project).where(Project.name.like("WithTargets%"))).first()
            assert proj is not None and proj.project_type == ProjectType.KEY_BASED

        # the list carries the badge + the type filter
        listing = admin.get("/projects").text
        assert "type=key_based" in listing, "the type filter chips must be rendered"
        assert "/projects/%d/keys" % proj.id in listing, "the Keys badge must link"  # noqa: UP031
        filtered = admin.get("/projects?type=key_based").text
        assert "WithTargets" in filtered
        assert admin.get("/projects?type=document").text.count("WithTargets") == 0

        # a DOCUMENT project is created exactly as before (no type posted at all)
        r = admin.post(
            "/projects",
            data={
                "name": "PlainDoc %d" % _uniq(),  # noqa: UP031
                "source_locale": "en-US",
                "target_locales": ["de-DE"],
                "customer_id": str(cust),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        with Session(engine) as s:
            plain = s.exec(select(Project).where(Project.name.like("PlainDoc%"))).first()
            assert plain.project_type == ProjectType.DOCUMENT, plain.project_type
    finally:
        _auth(False)
    print(
        "  [B] the type selector is offered, a key project needs >=1 target "
        "locale, the badge + filter work, and a typeless POST is still a "
        "document project OK"
    )


def test_key_project_page_has_no_document_list():
    cust = _mk_customer()
    kpid = _mk_key_project(cust)
    dpid = _mk_doc_project(cust)
    _add_key(kpid, "page::probe")
    client = TestClient(app)
    key_page = client.get("/projects/%d" % kpid).text  # noqa: UP031
    assert "Upload a document" not in key_page, "a key project must not offer a document upload"
    assert "<h2>Documents</h2>" not in key_page, "a key project must never show a document list"
    assert "/projects/%d/keys" % kpid in key_page, "it must link to the key grid"  # noqa: UP031
    doc_page = client.get("/projects/%d" % dpid).text  # noqa: UP031
    assert "Upload a document" in doc_page and "<h2>Documents</h2>" in doc_page, (
        "a DOCUMENT project's page must be unchanged"
    )
    print(
        "  [B] a key project's page shows the key grid link and NO document "
        "sections; a document project's page is unchanged OK"
    )


# ============================================================================
# [C] the CLI, local and over --remote
# ============================================================================
def test_cli_local_verbs():
    cust = _mk_customer()
    pid = _mk_key_project(cust)
    rc, p = _run_json(
        [
            "keys",
            "add",
            "--project",
            str(pid),
            "--name",
            "cli::greet",
            "--base",
            "Hello",
            "--namespace",
            "app",
        ]
    )
    assert rc == 0, p
    assert p["key"]["namespace"] == "app", p

    rc, p = _run_json(
        [
            "keys",
            "set",
            "--project",
            str(pid),
            "--name",
            "cli::greet",
            "--namespace",
            "app",
            "--locale",
            "de-DE",
            "--text",
            "Hallo",
        ]
    )
    assert rc == 0, p
    assert p["key"]["values"]["de-DE"][0]["text"] == "Hallo", p

    rc, p = _run_json(["keys", "list", "--project", str(pid)])
    assert rc == 0 and len(p["keys"]) == 1, p
    rc, p = _run_json(["keys", "list", "--project", str(pid), "--untranslated-in", "ru-RU"])
    assert rc == 0 and len(p["keys"]) == 1, p
    rc, p = _run_json(["keys", "list", "--project", str(pid), "--untranslated-in", "de-DE"])
    assert rc == 0 and p["keys"] == [], ("de-DE is translated", p)

    rc, p = _run_json(
        ["keys", "export", "--project", str(pid), "--locale", "de-DE", "--namespace", "app"]
    )
    assert rc == 0, p
    assert _json.loads(p["content"])["cli"]["greet"] == "Hallo", p

    # a destructive verb refuses without --yes in --json mode
    rc, p = _run_json(
        ["keys", "delete", "--project", str(pid), "--name", "cli::greet", "--namespace", "app"]
    )
    assert rc != 0, ("delete must refuse without --yes in machine mode", p)
    with Session(engine) as s:
        assert keys_svc.get_key(s, pid, "cli::greet", "app") is not None
    rc, p = _run_json(
        [
            "keys",
            "delete",
            "--project",
            str(pid),
            "--name",
            "cli::greet",
            "--namespace",
            "app",
            "--yes",
        ]
    )
    assert rc == 0, p
    with Session(engine) as s:
        assert keys_svc.get_key(s, pid, "cli::greet", "app") is None

    # a DOCUMENT project answers the same refusal a missing id gets
    dpid = _mk_doc_project(cust)
    rc, p = _run_json(["keys", "list", "--project", str(dpid)])
    assert rc != 0 and "not a key-based project" in (p.get("error") or ""), p
    print(
        "  [C] CLI add / set / list (+ value filters) / export / delete "
        "(confirm-gated) work locally; a document project is refused OK"
    )


def test_cli_can_create_a_key_project():
    """PARITY. Without `project create --type`, the ONLY way to make a key project
    would be the web dialog -- a feature with one surface, which the repo's parity law
    calls an incomplete feature. Covered on BOTH arms, plus the >=1 target rule and
    the kill switch, because each is enforced in a different place (the CLI body, the
    service) and a missing one is silent."""
    cust = _mk_customer()
    rc, p = _run_json(
        [
            "project",
            "create",
            "CliKeyProj %d" % _uniq(),  # noqa: UP031
            "--targets",
            "de-DE",
            "--customer-id",
            str(cust),
            "--type",
            "key_based",
        ]
    )
    assert rc == 0, p
    assert p["project_type"] == ProjectType.KEY_BASED, p
    pid = p["id"]
    rc, listed = _run_json(["keys", "list", "--project", str(pid)])
    assert rc == 0, ("the created project must be usable as a key project", listed)

    rc, p = _run_json(
        [
            "project",
            "create",
            "CliKeyNoTargets %d" % _uniq(),  # noqa: UP031
            "--customer-id",
            str(cust),
            "--type",
            "key_based",
        ]
    )
    assert rc != 0 and "at least one target language" in (p.get("error") or ""), p

    # the default is still a DOCUMENT project, byte-identical to before this batch
    rc, p = _run_json(
        [
            "project",
            "create",
            "CliDocProj %d" % _uniq(),  # noqa: UP031
            "--targets",
            "de-DE",
            "--customer-id",
            str(cust),
        ]
    )
    assert rc == 0 and p["project_type"] == ProjectType.DOCUMENT, p

    _switch(False)
    try:
        rc, p = _run_json(
            [
                "project",
                "create",
                "CliKeyOff %d" % _uniq(),  # noqa: UP031
                "--targets",
                "de-DE",
                "--customer-id",
                str(cust),
                "--type",
                "key_based",
            ]
        )
        assert rc != 0, ("the kill switch must refuse a key create from the CLI too", p)
    finally:
        _switch(True)

    # ...and over --remote, through POST /api/projects
    _auth(True)
    try:
        client = TestClient(app)
        tok = _key_for("keysurf-create-remote@x.com", scopes="api docs")
        _remote._set_transport(_mock_transport(client))
        try:
            rc, p = _run_json(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    tok,
                    "project",
                    "create",
                    "RemoteKeyProj %d" % _uniq(),  # noqa: UP031
                    "--targets",
                    "de-DE",
                    "--customer-id",
                    str(cust),
                    "--type",
                    "key_based",
                ]
            )
            assert rc == 0 and p["project_type"] == ProjectType.KEY_BASED, p
        finally:
            _remote._set_transport(None)
    finally:
        _auth(False)
    print(
        "  [C] `project create --type key_based` works locally and over --remote, "
        "refuses with no targets, refuses with the switch off, and leaves the "
        "default a document project OK"
    )


def test_cli_cursor_file_round_trip():
    cust = _mk_customer()
    pid = _mk_key_project(cust)
    _add_key(pid, "cursor::one", base="One")
    path = os.path.join(_TMP, "sync-%d.json" % _uniq())  # noqa: UP031

    rc, p = _run_json(["keys", "changes", "--project", str(pid), "--cursor-file", path])
    assert rc == 0 and p["entries"], p
    first_cursor = p["next_cursor"]
    assert _json.load(open(path))["next_cursor"] == first_cursor  # noqa: SIM115

    # a second poll with no new writes returns nothing and keeps the cursor
    rc, p = _run_json(["keys", "changes", "--project", str(pid), "--cursor-file", path])
    assert rc == 0 and p["entries"] == [], p
    assert _json.load(open(path))["next_cursor"] == first_cursor  # noqa: SIM115

    # ...and only the NEW writes come back afterwards
    _add_key(pid, "cursor::two", base="Two")
    rc, p = _run_json(["keys", "changes", "--project", str(pid), "--cursor-file", path])
    assert rc == 0 and p["entries"], p
    assert all(e["cursor"] > first_cursor for e in p["entries"]), p
    assert {e["name"] for e in p["entries"]} == {"cursor::two"}, p
    assert _json.load(open(path))["next_cursor"] == p["next_cursor"]  # noqa: SIM115

    # an explicit --since OVERRIDES the stored cursor (an operator re-reading)
    rc, p = _run_json(
        ["keys", "changes", "--project", str(pid), "--since", "0", "--cursor-file", path]
    )
    assert rc == 0 and {e["name"] for e in p["entries"]} == {"cursor::one", "cursor::two"}, p
    print(
        "  [C] `keys changes --cursor-file` bootstraps, advances, returns only "
        "new entries, and --since overrides the stored cursor OK"
    )


def test_cli_remote_twins():
    """Every verb over --remote really crosses to /api. Proven structurally: the
    in-process arm has NO scope check at all, so a `keys`-scope 403 can only happen
    on the other side of HTTP."""
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust)
        client = TestClient(app)
        good = _key_for("keysurf-remote@x.com", scopes="api keys")
        docs_only = _key_for("keysurf-docsonly@x.com", scopes="api docs")
        _remote._set_transport(_mock_transport(client))
        try:

            def remote(tok, *argv):
                return _run_json(["--remote", "http://127.0.0.1", "--token", tok, *argv])

            rc, p = remote(docs_only, "keys", "list", "--project", str(pid))
            assert rc != 0 and "keys-scoped" in (p.get("error") or ""), (
                "a docs-only token must be refused BY THE SERVER",
                p,
            )

            rc, p = remote(
                good,
                "keys",
                "add",
                "--project",
                str(pid),
                "--name",
                "rem::greet",
                "--base",
                "Hi",
                "--namespace",
                "app",
            )
            assert rc == 0, p
            with Session(engine) as s:
                assert keys_svc.get_key(s, pid, "rem::greet", "app") is not None, (
                    "the --remote add must have actually landed"
                )

            rc, p = remote(
                good,
                "keys",
                "set",
                "--project",
                str(pid),
                "--name",
                "rem::greet",
                "--namespace",
                "app",
                "--locale",
                "de-DE",
                "--text",
                "Hallo",
            )
            assert rc == 0, p
            assert p["key"]["values"]["de-DE"][0]["text"] == "Hallo", p

            rc, p = remote(good, "keys", "list", "--project", str(pid))
            assert rc == 0 and [k["name"] for k in p["keys"]] == ["rem::greet"], p

            # import over --remote (base64 through the JSON body)
            src = os.path.join(_TMP, "remote-%d.json" % _uniq())  # noqa: UP031
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(_json.dumps({"extra": "Extra"}))
            rc, p = remote(good, "keys", "import", "--project", str(pid), "--file", src)
            assert rc == 0 and p["created"] == 1, p

            out = os.path.join(_TMP, "remote-out-%d.json" % _uniq())  # noqa: UP031
            rc, p = remote(
                good,
                "keys",
                "export",
                "--project",
                str(pid),
                "--locale",
                "de-DE",
                "--namespace",
                "app",
                "-o",
                out,
            )
            assert rc == 0 and os.path.isfile(out), p
            assert _json.load(open(out))["rem"]["greet"] == "Hallo"  # noqa: SIM115

            cursor_path = os.path.join(_TMP, "remote-sync-%d.json" % _uniq())  # noqa: UP031
            rc, p = remote(
                good, "keys", "changes", "--project", str(pid), "--cursor-file", cursor_path
            )
            assert rc == 0 and p["entries"], p
            assert _json.load(open(cursor_path))["next_cursor"] == p["next_cursor"]  # noqa: SIM115

            rc, p = remote(
                good,
                "keys",
                "delete",
                "--project",
                str(pid),
                "--name",
                "rem::greet",
                "--namespace",
                "app",
                "--yes",
            )
            assert rc == 0, p
            with Session(engine) as s:
                assert keys_svc.get_key(s, pid, "rem::greet", "app") is None
        finally:
            _remote._set_transport(None)
    finally:
        _auth(False)
    print(
        "  [C] all seven verbs over --remote reach /api (a docs-only token is "
        "403'd server-side, which the in-process arm cannot do) OK"
    )


# ============================================================================
# [H] the /api write gate (role parity with the web routes)
# ============================================================================
def test_api_write_gate_matches_the_web_one():
    """A VIEWER's keys-scoped token READS but never WRITES.

    The web half of this is group [A]; without this, the /api group would be the one
    surface where a read-only role can mutate — historically the exact shape of the
    `/api` RBAC gap `_write_block` was introduced to close, and a scope check alone
    does NOT imply a role check (the token is correctly scoped; its owner just may not
    write)."""
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust)
        kid = _add_key(pid, "apigate::probe", base="Original")
        viewer = security.create_user(
            "keysurf-apiviewer@x.com", "keysurf-apiviewer@x.com", Role.VIEWER, PW
        )
        _row, tok = apikeys.create_key(viewer.id, "surf", scopes="api keys")
        # A VIEWER is not see-all, so make it a member: the claim under test is the
        # ROLE gate, and a caller that cannot even READ the project would prove it by
        # accident and keep passing with the role gate removed entirely.
        with Session(engine) as s:
            s.add(ProjectMember(project_id=pid, user_id=viewer.id))
            s.commit()
        client = TestClient(app)
        head = {"X-Loc-Token": tok}

        # reads are allowed
        for path in (
            "/api/projects/%d/keys" % pid,  # noqa: UP031
            "/api/projects/%d/changes" % pid,  # noqa: UP031
            "/api/keys/%d/values" % kid,  # noqa: UP031
        ):
            r = client.get(path, headers=head)
            assert r.status_code == 200, (path, r.status_code, r.text[:200])

        # ...every write is 403
        probes = [
            (
                "POST keys",
                lambda: client.post(
                    "/api/projects/%d/keys" % pid,  # noqa: UP031
                    headers=head,
                    json={"keys": [{"name": "sneak"}]},  # noqa: RUF100, UP031
                ),
            ),
            (
                "POST import",
                lambda: client.post(
                    "/api/projects/%d/keys/import" % pid,  # noqa: UP031
                    headers=head,
                    json={"filename": "x.json", "data": '{"sneak": "x"}'},
                ),
            ),
            (
                "PATCH key",
                lambda: client.patch(
                    "/api/keys/%d" % kid,  # noqa: UP031
                    headers=head,
                    json={"description": "sneak"},  # noqa: RUF100, UP031
                ),
            ),
            (
                "PUT value",
                lambda: client.put(
                    "/api/keys/%d/values/de-DE" % kid,  # noqa: UP031
                    headers=head,
                    json={"text": "sneak"},  # noqa: RUF100, UP031
                ),
            ),
            ("DELETE key", lambda: client.delete("/api/keys/%d" % kid, headers=head)),  # noqa: UP031
        ]
        for label, probe in probes:
            r = probe()
            assert r.status_code == 403, (label, r.status_code, r.text[:200])
        with Session(engine) as s:
            key = s.get(StringKey, kid)
            assert key.deleted_at is None and key.description == ""
            assert keys_svc.base_values(s, key) == {"": "Original"}
            assert keys_svc.get_key(s, pid, "sneak") is None
    finally:
        _auth(False)
    print(
        "  [H] a VIEWER's keys-scoped token reads the /api group but is 403'd on "
        "all 5 write routes, and writes nothing OK"
    )


def test_api_put_value_surfaces_a_protection_refusal():
    """F4 (adversarial review): PUT /api/keys/{id}/values/{locale} used to answer ok:true for
    a write the seam REFUSED (a human-approved value) -- a silent success-lie. It now answers
    409 with a named reason 'status_protected' and leaves the row byte-untouched, while an
    ordinary write to a non-final value still lands 200."""
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust)
        kid = _add_key(pid, "apiprot::probe", base="Original")
        tok = _key_for("keysurf-apiprot@x.com", scopes="api keys")
        client = TestClient(app)
        head = {"X-Loc-Token": tok}
        # a person APPROVES the de-DE value
        with Session(engine) as s:
            key = s.get(StringKey, kid)
            keys_svc.set_value(
                s,
                key,
                "de-DE",
                "Genehmigt",
                origin=keys_svc.ORIGIN_HUMAN,
                status=SegStatus.APPROVED,
            )
        # a machine (/api) write over it is REFUSED -> 409, named reason, row untouched
        r = client.put("/api/keys/%d/values/de-DE" % kid, headers=head, json={"text": "Maschinell"})  # noqa: UP031
        assert r.status_code == 409, (r.status_code, r.text[:300])
        body = r.json()
        assert body.get("ok") is False and body.get("reason") == "status_protected", body
        with Session(engine) as s:
            key = s.get(StringKey, kid)
            row = [  # noqa: RUF015
                v for v in keys_svc.values_for(s, key, "de-DE") if (v.plural_category or "") == ""
            ][0]
            assert (row.target_text, row.status) == ("Genehmigt", SegStatus.APPROVED), (
                row.target_text,
                row.status,
            )
        # an ordinary write to the OTHER (non-final) locale still lands 200
        r2 = client.put("/api/keys/%d/values/ru-RU" % kid, headers=head, json={"text": "Perevod"})  # noqa: UP031
        assert r2.status_code == 200, (r2.status_code, r2.text[:300])
        assert r2.json().get("ok") is True, r2.json()
    finally:
        _auth(False)
    print(
        "  [H] PUT /api/keys value 409s a protected write (named reason) and 200s a normal one OK"
    )


def test_api_put_plural_value_names_the_refused_forms():
    """F4-plural LOW (adversarial re-review): a PUT of a plural key where SOME forms are refused
    (a human-approved form) 409s and NAMES the refused categories in `refused`, while the OTHER
    forms still commit -- so the caller can tell which forms landed."""
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust)
        kid = _add_key(pid, "apiplural::probe", base="One", plural=True)
        tok = _key_for("keysurf-apiplural@x.com", scopes="api keys")
        client = TestClient(app)
        head = {"X-Loc-Token": tok}
        # a person APPROVES the de-DE `other` form
        with Session(engine) as s:
            key = s.get(StringKey, kid)
            keys_svc.set_value(
                s,
                key,
                "de-DE",
                "Genehmigt",
                origin=keys_svc.ORIGIN_HUMAN,
                plural_category="other",
                status=SegStatus.APPROVED,
            )
        # PUT both forms: `one` (non-final) must commit, `other` (approved) must be refused
        r = client.put(
            "/api/keys/%d/values/de-DE" % kid,  # noqa: UP031
            headers=head,
            json={"forms": {"one": "Eins-Maschine", "other": "Andere-Maschine"}},
        )
        assert r.status_code == 409, (r.status_code, r.text[:300])
        body = r.json()
        assert body.get("reason") == "status_protected", body
        assert body.get("refused") == ["other"], (
            "the 409 names the refused form(s)",
            body.get("refused"),
        )
        with Session(engine) as s:
            key = s.get(StringKey, kid)
            vals = {
                (v.plural_category or ""): v.target_text
                for v in keys_svc.values_for(s, key, "de-DE")
            }
            assert vals.get("one") == "Eins-Maschine", ("the non-final form committed", vals)
            assert vals.get("other") == "Genehmigt", ("the approved form was kept", vals)
    finally:
        _auth(False)
    print("  [H] PUT /api/keys plural value 409s naming the refused forms while others commit OK")


# ============================================================================
# [D] the kill switch, on all three surfaces
# ============================================================================
def test_kill_switch_off_everywhere():
    cust = _mk_customer()
    pid = _mk_key_project(cust)
    kid = _add_key(pid, "switch::probe")
    tok = _key_for("keysurf-switch@x.com")
    client = TestClient(app)
    _switch(False)
    try:
        for path in (
            "/projects/%d/keys" % pid,  # noqa: UP031
            "/projects/%d/keys/export?locale=de-DE" % pid,  # noqa: UP031
            "/projects/%d/keys/%d/delete" % (pid, kid),  # noqa: UP031
        ):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 404, (path, r.status_code)
        for path, data in (
            ("/projects/%d/keys/add" % pid, {"name": "x"}),  # noqa: UP031
            ("/projects/%d/keys/%d/value" % (pid, kid), {"locale": "de-DE", "text": "x"}),  # noqa: UP031
            ("/projects/%d/keys/translate" % pid, {"workflow_id": "1", "locale": "de-DE"}),  # noqa: UP031
        ):
            r = client.post(path, data=data, follow_redirects=False)
            assert r.status_code == 404, (path, r.status_code)

        for path in (
            "/api/projects/%d/keys" % pid,  # noqa: UP031
            "/api/projects/%d/changes" % pid,  # noqa: UP031
            "/api/keys/%d/values" % kid,  # noqa: UP031
        ):
            r = client.get(path, headers={"X-Loc-Token": tok})
            assert r.status_code == 404, (path, r.status_code, r.text)
        r = client.delete("/api/keys/%d" % kid, headers={"X-Loc-Token": tok})  # noqa: UP031
        assert r.status_code == 404, r.status_code

        for argv in (
            ["keys", "list", "--project", str(pid)],
            ["keys", "add", "--project", str(pid), "--name", "x"],
            ["keys", "changes", "--project", str(pid)],
        ):
            rc, p = _run_json(argv)
            assert rc != 0 and "disabled" in (p.get("error") or ""), (argv, p)

        # ...and the affordances are gone from the HTML
        page = client.get("/projects").text
        assert 'value="key_based"' not in page, (
            "the type selector must disappear with the switch off"
        )
        assert "type=key_based" not in page, "the type filter must disappear too"

        # a key project created BEFORE the switch went off is still not enumerable
        # through the key surfaces, and the create seam refuses a new one
        with Session(engine) as s:
            try:
                content.project_create(
                    s,
                    "SwitchOff %d" % _uniq(),  # noqa: UP031
                    source_locale="en-US",
                    target_locales="de-DE",
                    customer_id=str(cust),
                    project_type=ProjectType.KEY_BASED,
                )
                raise AssertionError(
                    "the service must refuse a key project while the switch is off"
                )
            except content.InvalidProjectTypeError:
                pass
    finally:
        _switch(True)
    print(
        "  [D] switch OFF: web 404s, /api 404s, the CLI refuses, the type "
        "selector + filter vanish, and the create seam refuses OK"
    )


# ============================================================================
# [E] ASCII-safe output
# ============================================================================
def test_cli_output_is_ascii():
    cust = _mk_customer()
    pid = _mk_key_project(cust)
    _add_key(pid, "ascii::probe", base="Hello")
    outputs = []
    for argv in (
        ["keys", "list", "--project", str(pid)],
        ["keys", "changes", "--project", str(pid)],
        ["keys", "add", "--project", str(pid), "--name", "ascii::two", "--base", "Two"],
        ["keys", "export", "--project", str(pid), "--locale", "de-DE", "--namespace", ""],
    ):
        rc, out = _run(argv)
        assert rc == 0, (argv, out)
        outputs.append(out)
    for out in outputs:
        bad = [ch for ch in out if ord(ch) > 126]
        assert not bad, ("non-ASCII in CLI output", bad[:5], out[:200])
    print("  [E] `keys` output is pure ASCII on every verb (Windows consoles) OK")


# ============================================================================
# [F] the two Wave B obligations
# ============================================================================
def _mk_workflow(name):
    with Session(engine) as s:
        wf = Workflow(name=name, scope="global", source_locale="en-US", target_locale="de-DE")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                step_type="llm_agent",
                provider="mock",
                name="translate",
                prompt_template="Translate: {text}",
            )
        )
        s.commit()
        return wf.id


def _queue_launch_probe(fake):
    """Observe execution after durable admission, using the real queued row id."""

    def execute(workflow_id, doc_id, locale, run_id=None, is_resume=False, slot=None):
        with Session(engine) as session:
            row = session.get(WorkflowRun, run_id)
            fake(session, workflow_id, doc_id, locale, created_by=row.created_by, slot=slot)
            row.status = "completed"
            session.add(row)
            session.commit()

    return execute


def _finish_queued_probes():
    import time

    from app.services import orchestrator

    orchestrator.ADMISSION.notify()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with Session(engine) as session:
            queued = session.exec(
                select(WorkflowRun.id).where(WorkflowRun.status == "queued")
            ).first()
        if queued is None and orchestrator.RUN_SLOTS.in_flight == 0:
            return
        time.sleep(0.02)
    raise AssertionError("queued execution probes did not drain")


def test_run_launcher_wraps_the_actor_scope():
    """SPEC section 5's Wave B obligation. FastAPI runs a sync handler on anyio's
    thread POOL, and `keys.actor_scope` inheritance stops at pools -- so the launcher
    must ENTER a scope around the launch or the run thread it starts inherits ""
    (or, worse, another request's leftover actor).

    Proven at the seam a reviewer can strip: the route's own source is not the claim,
    the ACTOR the launcher hands the raw thread is. `orchestrator._run_in_background` is
    patched to record `keys.current_actor()` as observed by the thread it starts."""
    import threading

    from app.services import orchestrator

    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    _add_key(pid, "actor::probe", base="Hello")
    wf_id = _mk_workflow("KeySurfActorWF %d" % _uniq())  # noqa: UP031
    seen = {}
    real = orchestrator._run_in_background

    def fake_start_run(session, workflow_id, doc_id, locale, created_by="", detail=None, slot=None):
        # The scheduler supplies one counted slot to each admitted execution.
        assert slot is not None, (
            "the admission scheduler must reserve capacity and hand a slot to each "
            "launch — an unslotted launch is either uncounted or refusable mid-loop",
            slot,
        )

        # Exactly what _run_in_background does: start a RAW thread and let the
        # inheritance hook copy the launching thread's actor onto it.
        def _work():
            seen["actor"] = keys_svc.current_actor()

        t = threading.Thread(target=_work)
        t.start()
        t.join()
        seen.setdefault("docs", []).append(doc_id)
        return 4242

    set_pieces(
        "app.services.orchestrator", "_run_in_background", _queue_launch_probe(fake_start_run)
    )
    _auth(True)
    try:
        admin = _login("keysurf-actor@x.com")
        r = admin.post(
            "/projects/%d/keys/translate" % pid,  # noqa: UP031
            data={"workflow_id": str(wf_id), "locale": "de-DE"},
            follow_redirects=False,
        )
        assert r.status_code == 303, (r.status_code, r.text[:300])
    finally:
        _finish_queued_probes()
        set_pieces("app.services.orchestrator", "_run_in_background", real)
        _auth(False)
    assert seen.get("docs"), ("the launcher started nothing", seen)
    assert seen.get("actor"), (
        "the run thread inherited NO actor -- the launcher did not wrap "
        "keys.actor_scope, so every write-back would be attributed to nobody",
        seen,
    )
    assert seen["actor"].startswith("workflow:"), seen
    assert "keysurf-actor@x.com" in seen["actor"], (
        "the label must name the human who asked for the run",
        seen,
    )
    print(
        "  [F] the key translate launcher enters keys.actor_scope, so the raw "
        "run thread inherits an attributable actor ('{}') OK".format(seen["actor"])
    )


def test_translate_all_locales_fans_out():
    """spec section 6's "(or all)": one control, one run per (namespace document x
    locale), each launched inside its OWN actor scope and through its OWN resolved
    workflow variant -- resolving the variant once outside the loop would silently run
    five languages through the first one's workflow."""
    import threading

    from app.services import orchestrator

    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE, fr-FR, ru-RU")
    _add_key(pid, "fanout::probe", base="Hello")
    wf_id = _mk_workflow("KeySurfFanoutWF %d" % _uniq())  # noqa: UP031
    launched = []
    real = orchestrator._run_in_background

    def fake_start_run(session, workflow_id, doc_id, locale, created_by="", detail=None, slot=None):
        # The scheduler supplies one counted slot to each admitted execution.
        assert slot is not None, (
            "the admission scheduler must reserve capacity and hand a slot to each "
            "launch — an unslotted launch is either uncounted or refusable mid-loop",
            slot,
        )
        actor = {}

        def _work():
            actor["seen"] = keys_svc.current_actor()

        t = threading.Thread(target=_work)
        t.start()
        t.join()
        launched.append((doc_id, locale, actor.get("seen")))
        return 9000 + len(launched)

    set_pieces(
        "app.services.orchestrator", "_run_in_background", _queue_launch_probe(fake_start_run)
    )
    _auth(True)
    try:
        admin = _login("keysurf-fanout@x.com")
        r = admin.post(
            "/projects/%d/keys/translate" % pid,  # noqa: UP031
            data={"workflow_id": str(wf_id), "locale": "all"},
            follow_redirects=False,
        )
        assert r.status_code == 303, (r.status_code, r.text[:200])
    finally:
        _finish_queued_probes()
        set_pieces("app.services.orchestrator", "_run_in_background", real)
        _auth(False)
    assert {loc for _d, loc, _a in launched} == {"de-DE", "fr-FR", "ru-RU"}, launched
    assert all(a and a.startswith("workflow:") and loc in a for _d, loc, a in launched), (
        "every fanned-out run must carry its OWN locale's actor label",
        launched,
    )
    print(
        "  [F] Translate -> all languages fans out one run per locale, each with "
        "its own actor scope and its own resolved workflow variant OK"
    )


def _mk_tm_mock_workflow(name):
    """A tm_match -> explicit-mock llm_agent workflow: the FT-incompatible shape whose
    mock-409 preflight still passes (the llm_agent step names 'mock' explicitly)."""
    with Session(engine) as s:
        wf = Workflow(name=name, scope="global", source_locale="en-US", target_locale="de-DE")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(WorkflowStep(workflow_id=wf.id, order_index=0, step_type="tm_match", name="TM"))
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=1,
                step_type="llm_agent",
                name="translate",
                provider="mock",
                prompt_template="Translate: {text}",
            )
        )
        s.commit()
        return wf.id


def _add_internal_fulltext_doc(pid, locale, filename="ns-fulltext"):
    """A FULL_TEXT internal key document (fmt='keys') with body content in `locale`, so
    translatable_documents returns it and it reaches the fan-out's per-doc guard."""
    with Session(engine) as s:
        doc = Document(
            project_id=pid,
            filename=filename,
            fmt=keys_svc.KEY_DOC_FMT,
            mode=TransMode.FULL_TEXT,
            status="new",
            skeleton={"units": [{"text": "A whole body of text."}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id,
                target_locale=locale,
                order_index=0,
                source_text="A whole body of text.",
                target_text="",
                status=SegStatus.NEW,
            )
        )
        s.commit()
        return doc.id


def _mk_mock_workflow(name):
    """A one-step llm_agent workflow whose translate step is EXPLICIT-mock, so the
    shared launch seam's mock-409 preflight (keys_svc.launch_translation) passes
    deterministically regardless of the install default provider."""
    with Session(engine) as s:
        wf = Workflow(name=name, scope="global", source_locale="en-US", target_locale="de-DE")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                step_type="llm_agent",
                name="translate",
                provider="mock",
                prompt_template="Translate: {text}",
            )
        )
        s.commit()
        return wf.id


def test_cli_keys_translate_local_and_remote():
    """Q8 parity: `keys translate` launches over a key project on the CLI (the /api
    route existed; the CLI verb did not). Local AND --remote both wrap the SAME shared
    seam keys_svc.launch_translation; a non-key project is refused; `--locale all` fans
    out across every active target locale."""
    import threading

    from app.services import orchestrator

    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE, fr-FR")
    _add_key(pid, "clt::probe", base="Hello")
    wf_id = _mk_mock_workflow("KeySurfCliTranslateWF %d" % _uniq())  # noqa: UP031

    launched = []
    real = orchestrator._run_in_background

    def fake_start_run(session, workflow_id, doc_id, locale, created_by="", detail=None, slot=None):
        # The scheduler supplies one counted slot to each admitted execution.
        assert slot is not None, (
            "the admission scheduler must reserve capacity and hand a slot to each "
            "launch — an unslotted launch is either uncounted or refusable mid-loop",
            slot,
        )
        seen = {}

        def _work():
            seen["actor"] = keys_svc.current_actor()

        t = threading.Thread(target=_work)
        t.start()
        t.join()
        launched.append((locale, seen.get("actor")))
        return 7000 + len(launched)

    set_pieces(
        "app.services.orchestrator", "_run_in_background", _queue_launch_probe(fake_start_run)
    )
    try:
        # (a) LOCAL, one locale -> one run, attributable actor
        launched.clear()
        rc, p = _run_json(
            [
                "keys",
                "translate",
                "--project",
                str(pid),
                "--workflow",
                str(wf_id),
                "--locale",
                "de-DE",
            ]
        )
        assert rc == 0, p
        assert p["launched"] and p["started"], p
        assert {loc for loc, _a in launched} == {"de-DE"}, launched
        assert all(a and a.startswith("workflow:") for _l, a in launched), launched

        # (b) LOCAL, locale=all fans out across every active target locale
        launched.clear()
        rc, p = _run_json(
            [
                "keys",
                "translate",
                "--project",
                str(pid),
                "--workflow",
                str(wf_id),
                "--locale",
                "all",
            ]
        )
        assert rc == 0, p
        assert {loc for loc, _a in launched} == {"de-DE", "fr-FR"}, launched

        # (c) a NON-key project is refused (honest error, no launch)
        dpid = _mk_doc_project(cust)
        launched.clear()
        rc, out = _run_both(
            [
                "keys",
                "translate",
                "--project",
                str(dpid),
                "--workflow",
                str(wf_id),
                "--locale",
                "de-DE",
            ]
        )
        assert rc != 0 and "not a key-based project" in out.lower(), (rc, out)
        assert launched == [], "a refused project must launch nothing"

        # (d) REMOTE parity: the --remote arm really crosses to
        #     POST /api/projects/{id}/keys/translate (the same seam).
        _auth(True)
        try:
            good = _key_for("keysurf-cltrans@x.com", scopes="api keys")
            client = TestClient(app)
            _remote._set_transport(_mock_transport(client))
            try:
                launched.clear()
                rc, p = _run_json(
                    [
                        "--remote",
                        "http://127.0.0.1",
                        "--token",
                        good,
                        "keys",
                        "translate",
                        "--project",
                        str(pid),
                        "--workflow",
                        str(wf_id),
                        "--locale",
                        "de-DE",
                    ]
                )
                assert rc == 0, p
                assert p["launched"] and not p["queued"], p
                _finish_queued_probes()
                assert {loc for loc, _a in launched} == {"de-DE"}, launched
            finally:
                _remote._set_transport(None)
        finally:
            _auth(False)
    finally:
        _finish_queued_probes()
        set_pieces("app.services.orchestrator", "_run_in_background", real)
    print(
        "  [C] `keys translate` launches locally (one locale + all), refuses a "
        "non-key project, and reaches /api over --remote OK"
    )


def test_cli_keys_translate_is_synchronous_in_process():
    """P-A2: the in-process `keys translate` CLI must FINISH its runs BEFORE the process
    exits. `start_run` drains each run on a `daemon=True` thread that a one-shot CLI kills
    the instant it exits, so an async launch here is a FALSE SUCCESS -- "Started" printed
    while the run is stuck RUNNING with zero translations and zero delta entries. The
    shared seam passes wait=True from the CLI and BLOCKS until every launched run is
    terminal. Proven by STORED STATE (translated value rows + change-feed entries), never
    the printed message. `--locale all` completes every locale. The /api route keeps
    wait=False and never drains, so it cannot block the event loop (Q2)."""
    from app.models import RunStatus
    from app.services import orchestrator

    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE, fr-FR")
    kid = _add_key(pid, "syncprobe", base="Hello world")
    wf_id = _mk_mock_workflow("KeySurfSyncWF %d" % _uniq())  # noqa: UP031

    # (a) ONE locale: after the in-process command RETURNS the value is translated, the
    #     run is terminal, and the change feed carries a value entry for that locale.
    rc, p = _run_json(
        ["keys", "translate", "--project", str(pid), "--workflow", str(wf_id), "--locale", "de-DE"]
    )
    assert rc == 0 and p["started"], p
    with Session(engine) as s:
        vals = keys_svc.values_for(s, s.get(StringKey, kid), "de-DE")
        assert vals and vals[0].target_text.strip(), (
            f"P-A2 false success: no translation landed after the CLI returned: {vals!r}"
        )
        run = s.exec(
            select(WorkflowRun).where(
                WorkflowRun.document_id == vals[0].document_id, WorkflowRun.target_locale == "de-DE"
            )
        ).first()
        assert run is not None and run.status == RunStatus.COMPLETED, (
            "the run must be terminal by the time the CLI returned",
            run and run.status,
        )
        feed = keys_svc.changes_since(s, pid, 0)
    assert any(e["entity"] == "value" and e["target_locale"] == "de-DE" for e in feed["entries"]), (
        "P-A2: the change feed has no value entry for the translated locale",
        feed,
    )

    # (b) --locale all: the drain must ITERATE over every launched run before returning.
    #     Asserted deterministically by spying on observe_run (F1) -- NOT by hoping the
    #     un-drained daemon happens to finish on its own in this in-process harness. The
    #     spy delegates to the real observer, so the translations still land for real.
    drained = []
    _real_obs = orchestrator.observe_run

    def _spy_obs(run_id, *a, **k):
        drained.append(run_id)
        return _real_obs(run_id, *a, **k)

    set_pieces("app.services.orchestrator", "observe_run", _spy_obs)
    try:
        rc, p = _run_json(
            [
                "keys",
                "translate",
                "--project",
                str(pid),
                "--workflow",
                str(wf_id),
                "--locale",
                "all",
            ]
        )
    finally:
        set_pieces("app.services.orchestrator", "observe_run", _real_obs)
    assert rc == 0, p
    assert len(p["started"]) == 2, ("--locale all must launch one run per locale", p)
    assert sorted(drained) == sorted(p["started"]), (
        "F1: the drain must iterate EVERY launched run before returning (not rely on the "
        "daemon finishing on its own)",
        drained,
        p["started"],
    )
    with Session(engine) as s:
        key = s.get(StringKey, kid)
        for loc in ("de-DE", "fr-FR"):
            vals = keys_svc.values_for(s, key, loc)
            assert vals and vals[0].target_text.strip(), (
                f"P-A2: --locale all left {loc} untranslated: {vals!r}"
            )

    # (c) THE /api POSTURE. wait=False (the route default) must NOT drain -- a blocking
    #     drain on the event loop is exactly what Q2 forbids. wait=True drains every run
    #     it launched. Proven by whether the shared seam reaches observe_run, with
    #     start_run stubbed so nothing real launches for this sub-check.
    calls = {"observe": 0}
    real_start, real_obs = orchestrator._run_in_background, orchestrator.observe_run

    def fake_start_run(session, workflow_id, doc_id, locale, created_by="", detail=None, slot=None):
        # The scheduler supplies one counted slot to each admitted execution.
        assert slot is not None, (
            "the admission scheduler must reserve capacity and hand a slot to each "
            "launch — an unslotted launch is either uncounted or refusable mid-loop",
            slot,
        )
        fake_start_run.n += 1
        return 9000 + fake_start_run.n

    fake_start_run.n = 0

    def fake_observe_run(run_id, *a, **k):
        calls["observe"] += 1
        return iter(())

    set_pieces(
        "app.services.orchestrator", "_run_in_background", _queue_launch_probe(fake_start_run)
    )
    set_pieces("app.services.orchestrator", "observe_run", fake_observe_run)
    try:
        with Session(engine) as s:
            proj = s.get(Project, pid)
            out_async = keys_svc.launch_translation(
                s, proj, wf_id, "de-DE", actor="cli", wait=False
            )
            assert out_async["launched"] and calls["observe"] == 0, (
                "the /api posture (wait=False) must NOT drain -- it would block the event loop",
                out_async,
                calls,
            )
            s.commit()
            _finish_queued_probes()
            s.expire_all()
            out_sync = keys_svc.launch_translation(s, proj, wf_id, "de-DE", actor="cli", wait=True)
            assert out_sync["started"] and calls["observe"] == len(out_sync["started"]), (
                "wait=True must drain every launched run",
                out_sync,
                calls,
            )
    finally:
        _finish_queued_probes()
        set_pieces("app.services.orchestrator", "_run_in_background", real_start)
        set_pieces("app.services.orchestrator", "observe_run", real_obs)
    # (d) F3: a drained run that FAILED (or is left RUNNING at the observer's 900s
    #     ceiling) is a FALSE SUCCESS unless the CLI says so. Force a failed verdict and
    #     assert the CLI WARNS + exits non-zero + reports ok:false, instead of printing a
    #     clean "Started". This kills the "drain iterates but discards the status" residual.
    real_start2, real_obs2 = orchestrator._run_in_background, orchestrator.observe_run

    def failing_start(session, workflow_id, doc_id, locale, created_by="", detail=None, slot=None):
        # The scheduler supplies one counted slot to each admitted execution.
        assert slot is not None, slot

        return 8200

    def failing_observe(run_id, *a, **k):
        # observe_run's terminal `closed` event carries the run's RunStatus; here FAILED.
        yield None, {"type": "closed", "run_id": run_id, "status": "failed"}

    set_pieces(
        "app.services.orchestrator", "_run_in_background", _queue_launch_probe(failing_start)
    )
    set_pieces("app.services.orchestrator", "observe_run", failing_observe)
    try:
        rc, p = _run_json(
            [
                "keys",
                "translate",
                "--project",
                str(pid),
                "--workflow",
                str(wf_id),
                "--locale",
                "de-DE",
            ]
        )
    finally:
        _finish_queued_probes()
        set_pieces("app.services.orchestrator", "_run_in_background", real_start2)
        set_pieces("app.services.orchestrator", "observe_run", real_obs2)
    assert rc != 0, ("F3: a drained FAILED run must make the CLI exit non-zero", rc, p)
    assert p.get("ok") is False, ("F3: the json payload must report ok:false", p)
    assert p.get("drain_incomplete") and p["drain_incomplete"][0]["status"] == "failed", (
        "F3: the failed run must be named in drain_incomplete",
        p,
    )

    print(
        "  [A2-sync] the in-process `keys translate` finishes its runs (translated "
        "values + delta entries) before returning; a FAILED drain warns + exits "
        "non-zero; /api stays async OK"
    )


def test_213_keys_translate_conflicts_are_reported_on_every_surface():
    """GitHub #213 fix round 1 (HIGH): the key fan-out's `conflicts` list (a run of a
    DIFFERENT workflow already holds that document + locale) was built in the service
    and DROPPED by every surface -- the web even answered "Nothing to translate into
    de-DE yet, add keys first." for a locale that had keys AND a live rival run. Now:
    a free locale starts while the conflicting pair is reported (web flash, /api body
    `conflicts`, both CLI arms); an ALL-conflicted fan-out is a clean 409 on /api and
    a non-zero CLI exit naming the running run; "add keys first" is never shown for a
    conflicted locale."""
    import time as _time
    from datetime import datetime as _dt
    from urllib.parse import unquote as _unquote

    from app.models import RunStatus, WorkflowRun

    planted: list = []

    def _plant_rival(pid, wf_id, locale):
        """A fresh RUNNING run of `wf_id` on the project's namespace document."""
        with Session(engine) as s:
            doc = keys_svc.translatable_documents(s, s.get(Project, pid))[0]
            run = WorkflowRun(
                workflow_id=wf_id,
                document_id=doc.id,
                target_locale=locale,
                status=RunStatus.RUNNING,
                created_by="213-fixture",
                started_at=_dt.utcnow(),
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            planted.append(run.id)
            return run.id

    def _release(*run_ids):
        with Session(engine) as s:
            for rid in run_ids:
                row = s.get(WorkflowRun, rid)
                if row is not None and row.status not in (
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                ):
                    row.status = RunStatus.CANCELLED
                    row.finished_at = _dt.utcnow()
                    s.add(row)
            s.commit()

    def _wait_terminal(pid, timeout=120):
        doc_ids = []
        with Session(engine) as s:
            doc_ids = [d.id for d in keys_svc.translatable_documents(s, s.get(Project, pid))]
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            with Session(engine) as s:
                rows = s.exec(select(WorkflowRun).where(WorkflowRun.document_id.in_(doc_ids))).all()
            if all(
                r.status
                in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.PAUSED)
                for r in rows
            ):
                return
            _time.sleep(0.05)
        raise AssertionError(f"runs on project {pid} never all reached a terminal state")

    _auth(False)
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE, fr-FR")
    _add_key(pid, "overlap::surf", base="Hello there.")
    wf_held = _mk_mock_workflow("Key213HeldWF %d" % _uniq())  # noqa: UP031
    wf_launch = _mk_mock_workflow("Key213LaunchWF %d" % _uniq())  # noqa: UP031
    client = TestClient(app)
    try:
        # --- WEB, partial fan-out: fr-FR starts, de-DE's conflict is NAMED in the
        #     notice, and the false "add keys first" line is gone.
        held = _plant_rival(pid, wf_held, "de-DE")
        r = client.post(
            "/projects/%d/keys/translate" % pid,  # noqa: UP031
            data={"workflow_id": wf_launch, "locale": "all"},
            follow_redirects=False,
        )
        assert r.status_code == 303, (r.status_code, r.text[:200])
        loc = _unquote(r.headers.get("location", ""))
        assert f"run #{held}" in loc, ("the web notice must name the running run", loc)
        assert "de-DE" in loc and "cancel" in loc.lower(), loc
        assert "add keys first" not in loc, (
            "a conflicted locale must never be told to add keys first",
            loc,
        )
        _release(held)
        _wait_terminal(pid)

        # --- WEB, every locale conflicting: the err flash names BOTH runs and still
        #     never says "Nothing to translate ... add keys first".
        held_a = _plant_rival(pid, wf_held, "de-DE")
        held_b = _plant_rival(pid, wf_held, "fr-FR")
        r = client.post(
            "/projects/%d/keys/translate" % pid,  # noqa: UP031
            data={"workflow_id": wf_launch, "locale": "all"},
            follow_redirects=False,
        )
        assert r.status_code == 303, (r.status_code, r.text[:200])
        loc = _unquote(r.headers.get("location", ""))
        assert f"run #{held_a}" in loc and f"run #{held_b}" in loc, loc
        assert "Nothing to translate" not in loc, loc
        with Session(engine) as s:
            inflight = s.exec(
                select(WorkflowRun).where(
                    WorkflowRun.workflow_id == wf_launch,
                    WorkflowRun.status.in_(("running", "pending", "queued", "paused")),
                )
            ).all()
        assert not inflight, "an all-conflicted web fan-out queued, attached or launched a run"

        # --- /api, all conflicting: 409 with the single-run machine shape.
        _auth(True)
        tok = _key_for("keysurf-213conflict@x.com", scopes="api keys")
        r = client.post(
            "/api/projects/%d/keys/translate" % pid,  # noqa: UP031
            headers={"X-Loc-Token": tok},
            json={"workflow_id": wf_launch, "locale": "all"},
        )
        assert r.status_code == 409, (r.status_code, r.text[:300])
        b = r.json()
        assert b["ok"] is False and b["code"] == "run_conflict", b
        assert b["run_id"] in (held_a, held_b), b
        assert f"run #{held_a}" in b["error"] and f"run #{held_b}" in b["error"], b
        assert {c["locale"] for c in b["conflicts"]} == {"de-DE", "fr-FR"}, b
        assert all(c["code"] == "run_conflict" for c in b["conflicts"]), b
        assert not b["launched"] and not b["started"], b

        # --- /api, mixed: release fr-FR, retry -- 200, fr-FR starts, the de-DE
        #     conflict is reported alongside with the running run's id.
        _release(held_b)
        r = client.post(
            "/api/projects/%d/keys/translate" % pid,  # noqa: UP031
            headers={"X-Loc-Token": tok},
            json={"workflow_id": wf_launch, "locale": "all"},
        )
        assert r.status_code == 200, (r.status_code, r.text[:300])
        b = r.json()
        assert b["ok"] is True, b
        assert len(b["conflicts"]) == 1 and b["conflicts"][0]["locale"] == "de-DE", b
        assert b["conflicts"][0]["run_id"] == held_a, b
        assert b["launched"], ("the free locale must start", b)
        _release(held_a)
        _wait_terminal(pid)

        # --- CLI local, mixed: exit 0 (the free locale ran), the conflict line
        #     printed with the running run's id, no "Nothing to translate".
        held = _plant_rival(pid, wf_held, "de-DE")
        rc, out = _run_both(
            [
                "keys",
                "translate",
                "--project",
                str(pid),
                "--workflow",
                str(wf_launch),
                "--locale",
                "all",
            ]
        )
        assert rc == 0, (rc, out)
        assert f"run #{held}" in out, ("the CLI must print the conflict line", out)
        assert "Nothing to translate" not in out, out
        rcj, pj = _run_json(
            [
                "keys",
                "translate",
                "--project",
                str(pid),
                "--workflow",
                str(wf_launch),
                "--locale",
                "all",
            ]
        )
        assert rcj == 0 and pj["ok"] is True, (rcj, pj)
        assert any(c["run_id"] == held for c in pj["conflicts"]), pj
        _release(held)
        _wait_terminal(pid)

        # --- CLI local, all conflicting: non-zero, conflicts named, ok:false.
        held_a = _plant_rival(pid, wf_held, "de-DE")
        held_b = _plant_rival(pid, wf_held, "fr-FR")
        rc, out = _run_both(
            [
                "keys",
                "translate",
                "--project",
                str(pid),
                "--workflow",
                str(wf_launch),
                "--locale",
                "all",
            ]
        )
        assert rc != 0, (rc, out)
        assert f"run #{held_a}" in out and f"run #{held_b}" in out, out
        assert "Nothing to translate" not in out, out

        # --- CLI --remote, all conflicting: the 409 back out, server sentence
        #     verbatim (the #213 _decode passthrough), non-zero.
        try:
            _remote._set_transport(_mock_transport(client))
            rc, out = _run_both(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    tok,
                    "keys",
                    "translate",
                    "--project",
                    str(pid),
                    "--workflow",
                    str(wf_launch),
                    "--locale",
                    "all",
                ]
            )
            assert rc != 0, (rc, out)
            assert f"run #{held_a}" in out and f"run #{held_b}" in out, out
            assert "server:" not in out, (
                "the run_conflict sentence rides verbatim, no 'server:' prefix",
                out,
            )
        finally:
            _remote._set_transport(None)
    finally:
        _release(*planted)
        _auth(False)
    print(
        "  #213 fix 1 (HIGH): keys-fan-out conflicts surface everywhere -- web flash, "
        "/api `conflicts` + 409 when everything conflicts, CLI local + --remote print "
        "the lines and exit non-zero only when nothing started; no false 'add keys "
        "first' OK"
    )


def test_337_keys_translate_empty_locale_rest_conflicted_is_409():
    """GitHub #337 N-3: /api keys translate with locale=all where ONE target language
    has no content (skipped) and EVERY OTHER language conflicts with another workflow's
    run answered 200 ok:true (listing the conflicts) -- but NOTHING launched, so the
    whole call was refused and it must be a 409 run_conflict like the all-conflict case
    (the empty locale still rides in `skipped`)."""
    from datetime import datetime as _dt

    from app.models import RunStatus

    planted: list = []
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE, fr-FR, es-ES")
    _add_key(pid, "n337::probe", base="Hello there.")
    wf_held = _mk_mock_workflow("KeyN337HeldWF %d" % _uniq())  # noqa: UP031
    wf_launch = _mk_mock_workflow("KeyN337LaunchWF %d" % _uniq())  # noqa: UP031
    with Session(engine) as s:
        project = s.get(Project, pid)
        doc = keys_svc.translatable_documents(s, project)[0]
        # Empty out es-ES: with no segments for that locale it is `skipped`, the
        # service's own no-content answer, never a launch.
        for seg in s.exec(
            select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == "es-ES")
        ).all():
            s.delete(seg)
        for loc in ("de-DE", "fr-FR"):
            run = WorkflowRun(
                workflow_id=wf_held,
                document_id=doc.id,
                target_locale=loc,
                status=RunStatus.RUNNING,
                created_by="337-fixture",
                started_at=_dt.utcnow(),
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            planted.append(run.id)
    _auth(True)
    try:
        tok = _key_for("keysurf-337n3@x.com", scopes="api keys")
        client = TestClient(app)
        r = client.post(
            "/api/projects/%d/keys/translate" % pid,  # noqa: UP031
            headers={"X-Loc-Token": tok},
            json={"workflow_id": wf_launch, "locale": "all"},
        )
        assert r.status_code == 409, (r.status_code, r.text[:300])
        b = r.json()
        assert b["ok"] is False and b["code"] == "run_conflict", b
        assert b["run_id"] in planted, b
        assert {c["locale"] for c in b["conflicts"]} == {"de-DE", "fr-FR"}, b
        assert all(c["code"] == "run_conflict" for c in b["conflicts"]), b
        assert not b["launched"] and not b["started"], b
        assert b["skipped"] == ["es-ES"], ("the empty locale rides in skipped", b)
    finally:
        with Session(engine) as s:
            for rid in planted:
                row = s.get(WorkflowRun, rid)
                if row is not None and row.status not in (
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                ):
                    row.status = RunStatus.CANCELLED
                    row.finished_at = _dt.utcnow()
                    s.add(row)
            s.commit()
        _auth(False)
    print(
        "  #337 N-3: /api fan-out with one empty locale + every other locale "
        "conflicted is a 409 run_conflict (nothing launched) OK"
    )


def test_api_keys_translate_stays_async():
    """F2: POST /api/projects/{id}/keys/translate must launch ASYNC -- it must NOT pass
    wait=True into the shared seam, or it would drain runs on the event loop (Q2). Pins
    the route posture at the seam boundary: a future wait=True on the route dies here."""
    from app.services import orchestrator

    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust, targets="de-DE")
        _add_key(pid, "asyncprobe", base="Hello")
        wf_id = _mk_mock_workflow("KeySurfAsyncWF %d" % _uniq())  # noqa: UP031
        tok = _key_for("keysurf-async@x.com", scopes="api keys")

        seen = {}
        real_launch = keys_svc.launch_translation
        real_start = orchestrator._run_in_background

        def spy_launch(
            session, project, workflow_id, locale, *, actor, wait=False, ip=None, **extra
        ):
            # default MIRRORS the seam's own default, so "the route passed nothing" is
            # recorded as wait=False and a future explicit wait=True is caught.
            seen["wait"] = wait
            return real_launch(
                session, project, workflow_id, locale, actor=actor, wait=wait, ip=ip, **extra
            )

        def fake_start(
            session,
            workflow_id,
            doc_id,
            locale,
            created_by="",
            detail=None,
            slot=None,
        ):
            # The scheduler supplies one counted slot to each admitted execution.
            assert slot is not None, slot

            # keep the route hermetic: no real daemon run for this posture probe.
            return 8100

        set_pieces("app.services.keys", "launch_translation", spy_launch)
        set_pieces(
            "app.services.orchestrator", "_run_in_background", _queue_launch_probe(fake_start)
        )
        try:
            client = TestClient(app)
            r = client.post(
                "/api/projects/%d/keys/translate" % pid,  # noqa: UP031
                headers={"X-Loc-Token": tok},
                json={"workflow_id": wf_id, "locale": "de-DE"},
            )
            assert r.status_code == 200, (r.status_code, r.text[:200])
        finally:
            set_pieces("app.services.keys", "launch_translation", real_launch)
            _finish_queued_probes()
            set_pieces("app.services.orchestrator", "_run_in_background", real_start)
        assert seen.get("wait") is False, (
            "F2: the /api route must call launch_translation with wait=False (async) -- "
            "wait=True would drain runs on the event loop",
            seen,
        )
    finally:
        _auth(False)
    print("  [F2] POST /api keys/translate launches async (wait=False), never drains OK")


def test_deleted_keys_are_out_of_run_scope():
    """The other Wave B obligation: a `--cleanup` import followed by Translate must
    not spend provider budget on strings the customer just deleted. The exclusion is
    STRUCTURAL -- a deleted key's value rows are parked in a document no launcher
    offers -- so this test attacks the run scope itself, not a route's filter."""
    from app.services import orchestrator

    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    live = _add_key(pid, "run::live", base="Live")
    doomed = _add_key(pid, "run::doomed", base="Doomed")
    with Session(engine) as s:
        project = s.get(Project, pid)
        docs = keys_svc.translatable_documents(s, project)
        assert len(docs) == 1, docs
        before = orchestrator._load_segments(s, docs[0].id, "de-DE")
        assert {seg.key_id for seg in before} == {live, doomed}, before

        keys_svc.delete_key(s, s.get(StringKey, doomed), actor="test")
        docs = keys_svc.translatable_documents(s, project)
        after = []
        for d in docs:
            after.extend(orchestrator._load_segments(s, d.id, "de-DE"))
        assert {seg.key_id for seg in after} == {live}, (
            "a DELETED key's segments are still in run scope -- a translate run "
            "would spend budget on deleted content",
            [seg.key_id for seg in after],
        )
        # the rows are not destroyed: restore is lossless
        assert len(keys_svc.values_for(s, s.get(StringKey, doomed))) == 1
        keys_svc.restore_key(s, s.get(StringKey, doomed), actor="test")
        docs = keys_svc.translatable_documents(s, project)
        back = []
        for d in docs:
            back.extend(orchestrator._load_segments(s, d.id, "de-DE"))
        assert {seg.key_id for seg in back} == {live, doomed}, (
            "a RESTORED key must be translatable again",
            back,
        )
    print(
        "  [F] a deleted key's segments leave run scope (and come back on "
        "restore) without a single per-caller filter OK"
    )


# ============================================================================
# [G] internal key documents are invisible on the document surfaces
# ============================================================================
def _internal_doc_fixture(tag):
    """A key project with ONE live key and ONE soft-DELETED key, so BOTH internal
    documents exist: the namespace value store, and the TRASH document holding the
    deleted key's parked value rows (`keys.KEY_TRASH_FILENAME`).

    The [G] cells below address them by id, which is the only way a caller ever
    could — there is no listing anywhere that hands one out."""
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    live = _add_key(pid, f"{tag}::probe", base="Hello")
    doomed = _add_key(pid, f"{tag}::doomed", base="Doomed")
    with Session(engine) as s:
        # A translation on the doomed key, so a write to its parked row would be a
        # real content change rather than a no-op the seam might skip anyway.
        keys_svc.set_value(
            s,
            s.get(StringKey, doomed),
            "de-DE",
            "Verdammt",
            origin=keys_svc.ORIGIN_HUMAN,
            actor="fixture",
        )
        keys_svc.delete_key(s, s.get(StringKey, doomed), actor="fixture")
        project = s.get(Project, pid)
        ns_doc = keys_svc.internal_document(s, project, "", create=False)
        trash = keys_svc.trash_document(s, project, create=False)
        assert ns_doc is not None and trash is not None, (ns_doc, trash)
        assert keys_svc.is_trash_document(trash) and not keys_svc.is_trash_document(ns_doc), (
            "the fixture's two internal documents must be distinguishable"
        )
        live_seg = keys_svc.values_for(s, s.get(StringKey, live))[0]
        parked = keys_svc.values_for(s, s.get(StringKey, doomed))[0]
        assert parked.document_id == trash.id, (
            "delete_key must have PARKED the row -- the rest of [G] is about that"
        )
        return {
            "pid": pid,
            "ns_doc": ns_doc.id,
            "trash": trash.id,
            "live_seg": live_seg.id,
            "parked_seg": parked.id,
            "parked_text": parked.target_text,
            "parked_status": parked.status,
            "live_key": live,
            "doomed_key": doomed,
        }


def test_internal_documents_are_invisible():
    cust = _mk_customer()
    pid = _mk_key_project(cust)
    _add_key(pid, "hidden::probe", base="Hello")
    tok = _key_for("keysurf-hidden@x.com", scopes="api docs")
    client = TestClient(app)
    with Session(engine) as s:
        internal = s.exec(select(Document).where(Document.project_id == pid)).all()
        assert internal and all(keys_svc.is_internal_key_document(d) for d in internal), internal
        doc_id = internal[0].id
        fname = internal[0].filename

    # web project page + dashboard + /projects rows
    for path in ("/projects/%d" % pid, "/", "/projects"):  # noqa: UP031
        assert fname not in client.get(path).text, f"an internal key document leaked onto {path}"

    # a DIRECT fetch through the document routes is not found
    r = client.get("/documents/%d" % doc_id, follow_redirects=False)  # noqa: UP031
    assert r.status_code in (303, 404), (r.status_code, r.text[:200])
    r = client.get("/documents/%d/export?locale=de-DE" % doc_id, follow_redirects=False)  # noqa: UP031
    assert r.status_code in (303, 404), r.status_code

    # /api listings
    r = client.get("/api/projects/%d/documents" % pid, headers={"X-Loc-Token": tok})  # noqa: UP031
    assert r.status_code == 200 and r.json()["documents"] == [], r.text
    r = client.get("/api/projects/%d" % pid, headers={"X-Loc-Token": tok})  # noqa: UP031
    assert r.status_code == 200 and r.json()["documents"] == [], r.text

    # the CLI
    rc, p = _run_json(["doc", "list", str(pid)])
    assert rc == 0 and p["documents"] == [], p
    rc, p = _run_json(["project", "show", str(pid)])
    assert rc == 0 and p.get("documents") == [], p
    print(
        "  [G] internal key documents appear on NO document surface (web pages, "
        "/api listings, `doc list`, `project show`) and a direct fetch 404s OK"
    )


def test_no_run_can_target_an_internal_key_document():
    """[G] A-F1. The four RUN launchers that take a document id.

    The wave claimed "deleted keys are out of run scope" and implemented it by PARKING
    a tombstoned key's value rows in the trash document that `translatable_documents`
    never lists. That is only an exclusion for a caller who builds its scope FROM that
    list — and `POST /documents/{id}/run` did not: it resolved the id with a bare
    `session.get(Document)`, so a run could be pointed straight at the trash document
    and spend provider budget translating strings the customer had just deleted, with
    ZERO delta entries to show for it (a tombstoned key is silent by design, A9).

    Two layers, and this cell demands BOTH: the routes resolve an internal key document
    as missing (`deps._visible_doc` / api_cli `_visible_document`), and
    `orchestrator.start_run` REFUSES the trash document itself, which is what makes the
    exclusion a property of the seam rather than of the launchers that exist today."""
    from app.services import orchestrator

    fx = _internal_doc_fixture("runscope")
    wf_id = _mk_workflow("KeySurfRunScopeWF %d" % _uniq())  # noqa: UP031
    tok = _key_for("keysurf-runscope@x.com", scopes="api docs")
    client = TestClient(app)
    csrf_for(client)
    head = {"X-Loc-Token": tok}

    for label, doc_id in (("namespace document", fx["ns_doc"]), ("TRASH document", fx["trash"])):
        probes = [
            (
                "web /documents/{id}/run",
                lambda d=doc_id: client.post(
                    "/documents/%d/run" % d,  # noqa: UP031
                    data={"workflow_id": str(wf_id), "locale": "de-DE"},
                    follow_redirects=False,
                ),
            ),
            (
                "web /documents/{id}/run-batch",
                lambda d=doc_id: client.post(
                    "/documents/%d/run-batch" % d,  # noqa: UP031
                    data={"workflow_id": str(wf_id)},
                    follow_redirects=False,
                ),
            ),
            (
                "api /api/workflows/{id}/run",
                lambda d=doc_id: client.post(
                    "/api/workflows/%d/run" % wf_id,  # noqa: UP031
                    headers=head,
                    json={"doc_id": d, "locale": "de-DE"},
                ),
            ),
            (
                "api /api/workflows/{id}/run-batch",
                lambda d=doc_id: client.post(
                    "/api/workflows/%d/run-batch" % wf_id,  # noqa: UP031
                    headers=head,
                    json={"doc_id": d},  # noqa: RUF100, UP031
                ),
            ),
        ]
        for probe_label, probe in probes:
            r = probe()
            assert r.status_code in (303, 404), (label, probe_label, r.status_code, r.text[:300])
            if r.status_code == 303:
                # a redirect must be the 404 error page's, never /runs/<id>
                assert "/runs/" not in (r.headers.get("location") or ""), (
                    label,
                    probe_label,
                    r.headers.get("location"),
                )

    # ...and nothing ran: no WorkflowRun row exists for either internal document.
    with Session(engine) as s:
        rows = s.exec(
            select(WorkflowRun).where(WorkflowRun.document_id.in_([fx["ns_doc"], fx["trash"]]))
        ).all()
        assert not rows, (
            "a run row was created for an internal key document",
            [(r.id, r.document_id, r.status) for r in rows],
        )

        # THE SEAM ITSELF, not just the routes: start_run refuses the trash document.
        try:
            orchestrator.start_run(s, wf_id, fx["trash"], "de-DE")
            raise AssertionError(
                "orchestrator.start_run accepted the TRASH document -- the parking "
                "claim is only as strong as each launcher's memory"
            )
        except ValueError as exc:
            assert "deleted" in str(exc).lower(), str(exc)
        assert not s.exec(
            select(WorkflowRun).where(WorkflowRun.document_id == fx["trash"])
        ).all(), "start_run refused but still created a run row"
        # ...while a NAMESPACE document stays launchable, which is how the key grid's
        # own Translate control works. Without this the refusal above would pass just
        # as well if the seam had been broken outright.
        run_id = orchestrator.start_run(s, wf_id, fx["ns_doc"], "de-DE")
        assert run_id, run_id
    print(
        "  [G] no run can be launched on an internal key document through any of "
        "the 4 doc-addressed launchers, and orchestrator.start_run itself refuses "
        "the trash document while a namespace document still runs OK"
    )


def _parked_row(fx):
    """The soft-deleted key's parked (source, target, status), read fresh."""
    from app.models import Segment

    with Session(engine) as s:
        seg = s.get(Segment, fx["parked_seg"])
        return (seg.source_text, seg.target_text, seg.status)


def test_every_run_launcher_refuses_the_trash_document():
    """[G] W1 (re-review). A-F1's refusal was in the WRONG SEAM.

    `orchestrator.start_run` is ONE launcher of four, and the re-reviewer reproduced
    the other three running a soft-deleted key's parked values with ZERO delta
    entries to show for it:

      (a) `workflow run <trash> <locale>` -> `orchestrator.run_workflow` (the CLI
          resolved the document with a bare `session.get`);
      (b) `workflow run <trash> --all-locales` -> `batch_runs.start_batch` ->
          `_dispatch` -> `_run_member` -> `run_workflow_stream`, never touching
          `start_run` at all;
      (c) `translate_document` as a JOB -> `jobs._run_translate_document` ->
          `translate.pretranslate_stream`, reachable from `POST /api/jobs/run` with
          a plain DOCS-scoped token (also an A-F3 hole: `keys` is a separate grant)
          and from `job run` on the CLI.

    So the load-bearing refusal moved to `run_workflow_stream`, the seam all four
    workflow launchers funnel through (`start_run`'s own background thread included),
    with `jobs._run_translate_document` gated separately because it is a translate
    launcher that never goes near a workflow. THE SEAM IS THE CLAIM: every probe
    below asserts the refusal AND that the parked row is byte-unchanged afterwards,
    and the two service-level cells at the end fail even if every surface guard were
    reinstated one layer up."""
    from app.services import orchestrator

    fx = _internal_doc_fixture("launchers")
    wf_id = _mk_workflow("KeySurfLaunchersWF %d" % _uniq())  # noqa: UP031
    before = _parked_row(fx)
    trash, ns_doc = fx["trash"], fx["ns_doc"]
    with Session(engine) as s:
        cursor0 = keys_svc.changes_since(s, fx["pid"], 0, limit=1000)["next_cursor"]

    # (a) + (b): the two CLI workflow-run arms answer like a missing document.
    for label, argv in (
        ("workflow run", ["workflow", "run", str(trash), str(wf_id), "de-DE"]),
        (
            "workflow run --all-locales",
            ["workflow", "run", str(trash), str(wf_id), "--all-locales"],
        ),
    ):
        rc, out = _run_both(argv)
        assert rc != 0, (label, rc, out[:400])
        assert "no such document" in out.lower(), (label, out[:400])
        assert "Verdammt" not in out, (label, "parked content leaked", out[:400])
    # ...and a NAMESPACE document is refused the same way by these two verbs, exactly
    # like their /api twins (`_visible_document` hides every fmt="keys" document);
    # a key project translates from its own grid, which never hands out a doc id.
    rc, out = _run_both(["workflow", "run", str(ns_doc), str(wf_id), "de-DE"])
    assert rc != 0 and "no such document" in out.lower(), (rc, out[:400])

    # (c) POST /api/jobs/run with a DOCS-scoped token: 404, before any work.
    tok = _key_for("keysurf-launchers@x.com", scopes="api docs")
    client = TestClient(app)
    for label, doc_id in (("namespace document", ns_doc), ("TRASH document", trash)):
        r = client.post(
            "/api/jobs/run",
            headers={"X-Loc-Token": tok},
            json={
                "kind": "translate_document",
                "payload": {"document_id": doc_id, "locale": "de-DE"},
            },
        )
        assert r.status_code == 404, (label, r.status_code, r.text[:300])
        assert "Verdammt" not in r.text and "Hello" not in r.text, (label, r.text[:300])

    # (c'): a MALFORMED `document_id` in the payload -- an authenticated docs-scoped
    # caller sending a JSON object/array, or an oversized int, must still get a clean
    # 400/404, never a raw 500 out of `_payload_key_document_block`'s `Session.get`.
    for label, bad_doc_id in (("object", {}), ("array", []), ("huge int", 10**30)):
        r = client.post(
            "/api/jobs/run",
            headers={"X-Loc-Token": tok},
            json={
                "kind": "translate_document",
                "payload": {"document_id": bad_doc_id, "locale": "de-DE"},
            },
        )
        assert r.status_code in (400, 404), (
            f"a malformed document_id ({label}) raised instead of a clean refusal",
            r.status_code,
            r.text[:300],
        )

    # (c) `job run translate_document` on the CLI: a clean FAILED job, not a run.
    rc, out = _run_both(
        [
            "job",
            "run",
            "translate_document",
            "--payload",
            "document_id=%d" % trash,  # noqa: UP031
            "--payload",
            "locale=de-DE",
        ]
    )
    assert rc != 0, (rc, out[:400])
    assert "no such document" in out.lower(), out[:400]

    assert _parked_row(fx) == before, (
        "a launcher wrote to the soft-deleted key's parked row",
        before,
        _parked_row(fx),
    )
    with Session(engine) as s:
        assert not s.exec(select(WorkflowRun).where(WorkflowRun.document_id == trash)).all(), (
            "a run row exists for the trash document"
        )

        # THE SEAM, not the surfaces: run_workflow_stream is where start_run's own
        # thread, run_workflow, batch_runs._run_member and resume_run all arrive.
        try:
            list(orchestrator.run_workflow_stream(s, wf_id, trash, "de-DE"))
            raise AssertionError(
                "run_workflow_stream accepted the TRASH document -- the refusal is "
                "back on one launcher instead of on the seam all four share"
            )
        except ValueError as exc:
            assert "deleted" in str(exc).lower(), str(exc)
        try:
            orchestrator.run_workflow(s, wf_id, trash, "de-DE")
            raise AssertionError("run_workflow accepted the TRASH document")
        except ValueError as exc:
            assert "deleted" in str(exc).lower(), str(exc)
        # ...and a NAMESPACE document still streams, so this is a guard, not a break.
        events = list(orchestrator.run_workflow_stream(s, wf_id, ns_doc, "de-DE"))
        assert events, "the seam refused a legitimate namespace document too"
    assert _parked_row(fx) == before, _parked_row(fx)

    # The delta feed is the customer-visible proof: a tombstoned key emits nothing,
    # so a run over its parked rows would have been invisible spend (review A9).
    with Session(engine) as s:
        feed = keys_svc.changes_since(s, fx["pid"], cursor0, limit=1000)
        assert not [e for e in feed["entries"] if e.get("key_id") == fx["doomed_key"]], (
            "a launcher minted feed entries for a soft-deleted key",
            feed,
        )
    print(
        "  [G] all THREE run launchers that bypassed start_run refuse the trash "
        "document (CLI `workflow run`, `--all-locales`, and translate_document as "
        "a job on /api + CLI), the SEAM itself (run_workflow_stream / run_workflow) "
        "refuses it while a namespace document still streams, and the parked row is "
        "byte-unchanged OK"
    )


def test_the_editor_carve_out_stops_at_the_trash_document():
    """[G] A-F2/B-4. Spec 2.5 lets the CAT editor open a key segment on purpose. That
    carve-out was BLANKET, so it also opened the trash document: `GET /documents/{id}/
    editor` rendered a soft-deleted key's content and `POST /segments/{id}/save` wrote
    to it — silently, because a tombstoned key emits no `value` entries at all. The key
    grid renders exactly those rows read-only and `keys.set_value` refuses them, so two
    surfaces disagreed about whether a deleted key is editable."""
    fx = _internal_doc_fixture("editor")
    client = TestClient(app)
    csrf_for(client)

    # the carve-out still holds for a NAMESPACE document (the deliberate exception)
    r = client.get("/documents/%d/editor" % fx["ns_doc"], follow_redirects=False)  # noqa: UP031
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert "Hello" in r.text, "the editor must still open a LIVE key's segments"

    # ...and stops at the trash document
    r = client.get("/documents/%d/editor" % fx["trash"], follow_redirects=False)  # noqa: UP031
    assert r.status_code in (303, 404), (r.status_code, r.text[:300])
    assert "Verdammt" not in r.text, "the trash document's content leaked"

    # the two segment WRITE routes refuse a parked row...
    for label, path in (
        ("save", "/segments/%d/save" % fx["parked_seg"]),  # noqa: UP031
        ("approve", "/segments/%d/approve" % fx["parked_seg"]),  # noqa: UP031
    ):
        r = client.post(path, data={"target_text": "SNEAKED IN"})
        assert r.status_code == 404, (label, r.status_code, r.text[:200])
        assert not r.json().get("ok"), (label, r.text[:200])
    with Session(engine) as s:
        from app.models import Segment

        seg = s.get(Segment, fx["parked_seg"])
        assert seg.target_text == fx["parked_text"], (
            "a deleted key's value was overwritten through the editor",
            seg.target_text,
        )
        assert seg.status == fx["parked_status"], seg.status

    # ...and still accept a LIVE key's row, so this is a guard, not a broken route.
    r = client.post("/segments/%d/save" % fx["live_seg"], data={"target_text": "Hallo"})  # noqa: UP031
    assert r.status_code == 200 and r.json().get("ok"), (r.status_code, r.text[:200])
    print(
        "  [G] the editor carve-out opens a NAMESPACE key document and refuses the "
        "TRASH one; /segments/{id}/save + /approve refuse a soft-deleted key's "
        "parked row and change nothing, while a live key's row still saves OK"
    )


def test_a_docs_scoped_token_cannot_reach_key_internals():
    """[G] A-F3. The `keys` scope is separate from `docs` on purpose: a CI sync
    consumer has no business uploading documents, and a document integration has no
    business reading the whole string catalogue. Four DOCUMENT routes ignored that —
    they resolved the document with a bare `session.get` — so a plain docs-scoped token
    could read key values (the trash document's included), write into them via apply-TM
    and pass the translate authz check."""
    fx = _internal_doc_fixture("apidocs")
    tok = _key_for("keysurf-apidocs@x.com", scopes="api docs")
    client = TestClient(app)
    head = {"X-Loc-Token": tok}

    for label, doc_id in (("namespace document", fx["ns_doc"]), ("TRASH document", fx["trash"])):
        probes = [
            (
                "GET /api/documents/{id}/segments",
                lambda d=doc_id: client.get(
                    "/api/documents/%d/segments?detail=segments" % d,  # noqa: UP031
                    headers=head,  # noqa: RUF100, UP031
                ),
            ),
            (
                "POST /api/documents/{id}/apply-tm",
                lambda d=doc_id: client.post(
                    "/api/documents/%d/apply-tm" % d,  # noqa: UP031
                    headers=head,
                    json={"locale": "de-DE"},  # noqa: RUF100, UP031
                ),
            ),
            (
                "POST /api/documents/{id}/translate",
                lambda d=doc_id: client.post(
                    "/api/documents/%d/translate" % d,  # noqa: UP031
                    headers=head,
                    json={"targets": ["de-DE"]},  # noqa: RUF100, UP031
                ),
            ),
            (
                "GET /api/documents/{id}/download",
                lambda d=doc_id: client.get(
                    "/api/documents/%d/download?locale=de-DE" % d,  # noqa: UP031
                    headers=head,  # noqa: RUF100, UP031
                ),
            ),
        ]
        for probe_label, probe in probes:
            r = probe()
            assert r.status_code == 404, (label, probe_label, r.status_code, r.text[:300])
            assert "Verdammt" not in r.text and "Hello" not in r.text, (
                label,
                probe_label,
                "key content leaked",
                r.text[:300],
            )
    print(
        "  [G] a docs-scoped token gets a plain 404 from all 4 doc-addressed /api "
        "routes for BOTH internal key documents, and no key content leaks OK"
    )


def test_the_two_unguarded_segment_routes_refuse_a_deleted_key():
    """[G] W5 (re-review). `_deleted_key_block` was on `/segments/{id}/save` and
    `/approve` but NOT on the two other segment routes that reach a parked row:

      * `POST /segments/{id}/alternates` writes nothing, but it sends the deleted
        key's source to a provider THREE times and echoes it back in the response —
        provider spend plus a read of content the grid renders read-only;
      * `POST /segments/{id}/tag-repair` runs `apply=True` and WRITES `target_text`,
        silently, since a tombstoned key emits no `value` entries at all.

    Both now carry the same guard, and both still serve a LIVE key's row."""
    fx = _internal_doc_fixture("segroutes")
    client = TestClient(app)
    csrf_for(client)
    before = _parked_row(fx)

    for label, path in (
        ("alternates", "/segments/%d/alternates"),
        ("tag-repair", "/segments/%d/tag-repair"),
    ):
        r = client.post(path % fx["parked_seg"])
        assert r.status_code == 404, (label, r.status_code, r.text[:300])
        assert not r.json().get("ok"), (label, r.text[:300])
        assert "Verdammt" not in r.text and "Doomed" not in r.text, (
            label,
            "the parked value was echoed back",
            r.text[:300],
        )
    assert _parked_row(fx) == before, (
        "a segment route wrote to a parked row",
        before,
        _parked_row(fx),
    )

    # ...and a LIVE key's row still reaches both routes (a guard, not a broken route).
    r = client.post("/segments/%d/alternates" % fx["live_seg"])  # noqa: UP031
    assert r.status_code == 200 and "alternates" in r.json(), (r.status_code, r.text[:200])
    r = client.post("/segments/%d/tag-repair" % fx["live_seg"])  # noqa: UP031
    assert r.status_code in (200, 400), (r.status_code, r.text[:200])
    assert "restore it in the key grid" not in r.text, (
        "the guard fired on a LIVE key's row",
        r.text[:200],
    )
    print(
        "  [G] /segments/{id}/alternates and /segments/{id}/tag-repair refuse a "
        "soft-deleted key's parked row (no provider call, no write, nothing echoed) "
        "and still serve a live key's row OK"
    )


def test_the_cli_doc_verbs_have_the_key_document_gate():
    """[G] W6 (re-review). A-F3 closed the /api document routes and the web ones and
    left the CLI's third surface open (the dual-surface law is the point): every `doc`
    verb resolved its id through `cmd_doc._get_doc`, a bare `session.get`. So
    `doc show <trash>` printed a soft-deleted key's whole parked catalogue and exited
    0, and `doc apply-tm <trash>` WROTE to those rows — while both /api twins already
    answered 404."""
    fx = _internal_doc_fixture("cliverbs")
    before = _parked_row(fx)
    for label, argv in (
        ("doc show", ["doc", "show", "%d"]),
        ("doc apply-tm", ["doc", "apply-tm", "%d", "de-DE"]),
        ("doc export", ["doc", "export", "%d", "--locale", "de-DE"]),
        ("doc rename", ["doc", "rename", "%d", "sneaky.json"]),
        ("doc delete", ["doc", "delete", "%d", "--yes"]),
    ):
        for which, doc_id in (
            ("namespace document", fx["ns_doc"]),
            ("TRASH document", fx["trash"]),
        ):
            rc, out = _run_both([a % doc_id if "%d" in a else a for a in argv])
            assert rc != 0, (label, which, rc, out[:400])
            assert "no such document" in out.lower(), (label, which, out[:400])
            assert "Verdammt" not in out and "Doomed" not in out, (
                label,
                which,
                "the parked catalogue leaked",
                out[:400],
            )
    assert _parked_row(fx) == before, (
        "a CLI doc verb wrote to a parked row",
        before,
        _parked_row(fx),
    )
    with Session(engine) as s:
        assert s.get(Document, fx["trash"]) is not None, "`doc delete` reaped it"
        assert s.get(Document, fx["ns_doc"]) is not None, "`doc delete` reaped it"
    print(
        "  [G] all 5 by-id CLI `doc` verbs answer 'no such document' for BOTH "
        "internal key documents, exactly like their /api twins, and neither the "
        "parked rows nor the documents themselves are touched OK"
    )


# ============================================================================
# [I] the two lifecycle seams that reached internal key documents anyway
# ============================================================================
def test_a_key_project_takes_no_documents():
    """[I] A-F4. Project type is immutable (spec 2.1) and the two types are disjoint,
    but neither the web upload nor `/api` ingest checked it. A smuggled document was
    INVISIBLE in the UI (a key project's page renders the key grid, never a document
    list) while showing up in `/api/projects/{id}/documents`, and it falsified two of
    this wave's written claims at once: a key project never auto-completes, and its
    delivery bundle is empty. Refused in `ingest_document`, so all three surfaces
    inherit it."""
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    _add_key(pid, "upload::probe", base="Hello")
    tok = _key_for("keysurf-upload@x.com", scopes="api docs")
    client = TestClient(app)
    csrf_for(client)
    payload = b"Smuggled sentence one. Smuggled sentence two."

    r = client.post(
        "/projects/%d/documents" % pid,  # noqa: UP031
        files={"file": ("smuggled.txt", payload, "text/plain")},
        data={"mode": "segmented"},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    assert "/documents/" not in (r.headers.get("location") or ""), (
        "the web upload created a document inside a key project",
        r.headers.get("location"),
    )

    r = client.post(
        "/api/documents",
        headers={"X-Loc-Token": tok},
        files={"file": ("smuggled.txt", payload, "text/plain")},
        data={"project_id": str(pid)},
    )
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert "key-based" in r.text, r.text[:300]

    import tempfile

    path = os.path.join(tempfile.mkdtemp(dir=_TMP), "smuggled.txt")
    with open(path, "wb") as fh:
        fh.write(payload)
    # the CLI prints its refusal on STDERR (CliError), so capture both streams
    from contextlib import redirect_stderr

    err = io.StringIO()
    with redirect_stderr(err):
        rc, out = _run(["doc", "upload", str(pid), path])
    assert rc != 0, (rc, out)
    assert "key-based" in (out + err.getvalue()), (out, err.getvalue())

    with Session(engine) as s:
        docs = s.exec(select(Document).where(Document.project_id == pid)).all()
        assert docs and all(keys_svc.is_internal_key_document(d) for d in docs), (
            "a real document landed in a key project",
            [d.filename for d in docs],
        )
    print(
        "  [I] a key project refuses an uploaded document on all three surfaces "
        "(web / /api / CLI) and keeps only its internal documents OK"
    )


def test_retention_never_ages_out_a_key_catalogue():
    """[I] A-F5. `retention.purge` selected EVERY Document older than the cutoff. An
    internal key document is created ONCE, on the first key in its namespace, and its
    `created_at` never moves again — so on any install with retention on, a key
    project's whole catalogue was guaranteed to be destroyed `retention_days` after it
    was started, however live its keys were. Worse than silent: `delete_document` emits
    honest tombstones for the reaped values, so the delta feed would have told every
    consumer that a fully live catalogue had been deleted key by key."""
    from datetime import datetime, timedelta

    from app.models import Segment
    from app.services import retention

    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    kid = _add_key(pid, "retain::probe", base="Keep me")
    doc_pid = _mk_doc_project(cust)
    with Session(engine) as s:
        # age EVERY document in both projects well past any cutoff
        old = datetime.utcnow() - timedelta(days=900)
        docs = s.exec(select(Document)).all()
        for d in docs:
            d.created_at = old
            s.add(d)
        # ...and give the document project a real document to prove the sweep RUNS
        from app.models import DocStatus, TransMode
        from app.models import Document as _Doc

        victim = _Doc(
            project_id=doc_pid,
            filename="aged.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            status=DocStatus.NEW,
            skeleton={"units": [{"text": "Aged."}]},
            created_at=old,
        )
        s.add(victim)
        s.commit()
        s.refresh(victim)
        victim_id = victim.id
        key_doc_ids = [d.id for d in keys_svc.key_documents(s, pid)]
        assert key_doc_ids, "the fixture must have internal key documents"

    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    settings.invalidate_cache()
    try:
        result = retention.purge()
    finally:
        settings.set_many({"retention_enabled": "0", "retention_days": "0"})
        settings.invalidate_cache()

    assert result["documents"] >= 1, (
        "the sweep must actually have swept something, or this test proves nothing",
        result,
    )
    with Session(engine) as s:
        assert s.get(Document, victim_id) is None, (
            "the aged ORDINARY document survived -- the sweep did not run"
        )
        for did in key_doc_ids:
            assert s.get(Document, did) is not None, (
                f"retention reaped an internal key document ({did})"
            )
        key = s.get(StringKey, kid)
        assert key is not None and key.deleted_at is None, key
        rows = keys_svc.values_for(s, key)
        assert rows and all(s.get(Segment, r.id) is not None for r in rows), rows
        assert keys_svc.base_values(s, key) == {"": "Keep me"}, (
            "the key's catalogue content was destroyed by the retention sweep"
        )
        # ...and no consumer was told a live key had been deleted.
        feed = keys_svc.changes_since(s, pid, 0, 500)
        assert not [e for e in feed["entries"] if e["op"] == "deleted"], (
            "retention emitted DELETED entries for a live key's values",
            feed,
        )
    print(
        "  [I] a retention sweep that reaps an aged ordinary document leaves every "
        "internal key document, its values and its change feed untouched OK"
    )


# ============================================================================
# [J] the fix round's CLI / parity / audit cells
# ============================================================================
def test_a_cursor_file_from_another_project_bootstraps():
    """[J] B-1. `--cursor-file` defaults to `.locstudio-sync.json` in the CURRENT
    DIRECTORY and the changelog id is a GLOBAL sequence filtered per project, so a
    repo syncing two key projects had project B read project A's high-water mark, ask
    for everything after it and get NOTHING -- with `resync_required: false` (the
    cursor is ABOVE the floor, not below it) and the file rewritten with the same
    number. B never caught up, and the answer was indistinguishable from "nothing
    changed": the one thing this feed promises never to say wrongly."""
    cust = _mk_customer()
    pid_a = _mk_key_project(cust, targets="de-DE")
    pid_b = _mk_key_project(cust, targets="de-DE")
    # B is written FIRST and A afterwards, so A's next_cursor lands ABOVE every entry
    # B has -- the ORDER that hides B's whole feed behind A's high-water mark. (The
    # ids are a global sequence; only the ordering of the writes decides this.)
    _add_key(pid_b, "shared::b0", base="B0")
    for n in range(4):
        _add_key(pid_a, "shared::a%d" % n, base="A%d" % n)  # noqa: UP031
    path = os.path.join(_TMP, "shared-sync-%d.json" % _uniq())  # noqa: UP031

    rc, p = _run_json(["keys", "changes", "--project", str(pid_a), "--cursor-file", path])
    assert rc == 0 and p["entries"], p
    stored = _json.load(open(path))  # noqa: SIM115
    assert stored["project_id"] == pid_a, stored

    # ...now the SAME file is pointed at project B.
    rc, p = _run_json(["keys", "changes", "--project", str(pid_b), "--cursor-file", path])
    assert rc == 0, p
    names = {e["name"] for e in p["entries"]}
    assert "shared::b0" in names, (
        "project B's feed was swallowed by project A's cursor -- a consumer that "
        "polls two projects with the default cursor path never catches up",
        p,
    )
    assert not p["resync_required"], p
    # the file now belongs to B, so B's next poll is a normal incremental one
    assert _json.load(open(path))["project_id"] == pid_b  # noqa: SIM115
    rc, p2 = _run_json(["keys", "changes", "--project", str(pid_b), "--cursor-file", path])
    assert rc == 0 and p2["entries"] == [], p2
    print(
        "  [J] a --cursor-file holding ANOTHER project's cursor bootstraps from 0 "
        "instead of silently swallowing this project's feed OK"
    )


def test_remote_keys_add_refuses_a_duplicate_exactly_like_the_local_arm():
    """[J] B-2. `POST /api/projects/{id}/keys` is the BULK UPSERT route (partial
    success by contract, so a CI push of 4,000 keys never loses 3,999 to one typo).
    Dispatching a single `keys add` at it turned the local arm's loud DuplicateKeyError
    into a SILENT overwrite of the existing BASE VALUE -- two surfaces of one verb
    answering the same input with "refused" and "your source string is gone"."""
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    _add_key(pid, "dup::probe", base="ORIGINAL")

    rc_local, p_local = _run_json(
        ["keys", "add", "--project", str(pid), "--name", "dup::probe", "--base", "OVERWRITTEN"]
    )
    assert rc_local != 0, (rc_local, p_local)

    _auth(True)
    try:
        client = TestClient(app)
        tok = _key_for("keysurf-dupremote@x.com", scopes="api keys")
        _remote._set_transport(_mock_transport(client))
        try:
            rc_remote, p_remote = _run_json(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    tok,
                    "keys",
                    "add",
                    "--project",
                    str(pid),
                    "--name",
                    "dup::probe",
                    "--base",
                    "OVERWRITTEN",
                ]
            )
        finally:
            _remote._set_transport(None)
    finally:
        _auth(False)
    assert rc_remote == rc_local, (rc_remote, rc_local)
    assert p_remote.get("error") == p_local.get("error"), (p_remote, p_local)
    assert (
        p_remote.get("error")
        == "the key name 'dup::probe' is not available in the default namespace."
    ), p_remote
    with Session(engine) as s:
        key = keys_svc.get_key(s, pid, "dup::probe")
        assert keys_svc.base_values(s, key) == {"": "ORIGINAL"}, (
            "the --remote add overwrote the base value it should have refused",
            keys_svc.base_values(s, key),
        )
    print(
        "  [J] `keys add --remote` on an existing key refuses with the SAME message "
        "and exit code as the local arm, and overwrites nothing OK"
    )


def test_restore_and_edit_are_headless_on_both_arms():
    """[J] B-3. `keys import --cleanup` is a HEADLESS verb that soft-deletes keys, so
    the operator most likely to need Restore was the one who had no way to run it --
    restore was web-only, and the whole metadata/identity/plural half of the grid had
    no command at all. Both arms, because a verb that only works in-process is half a
    verb (the parity law)."""
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    _add_key(pid, "hl::probe", base="Hello")

    # --- LOCAL ---------------------------------------------------------------
    rc, p = _run_json(
        [
            "keys",
            "edit",
            "--project",
            str(pid),
            "--name",
            "hl::probe",
            "--description",
            "greeting",
            "--tags",
            "ui,home",
            "--char-limit",
            "40",
            "--archive",
        ]
    )
    assert rc == 0, p
    assert p["key"]["description"] == "greeting" and p["key"]["is_archived"], p
    assert p["key"]["tags"] == ["ui", "home"] and p["key"]["char_limit"] == 40, p

    rc, p = _run_json(
        [
            "keys",
            "edit",
            "--project",
            str(pid),
            "--name",
            "hl::probe",
            "--rename",
            "hl::renamed",
            "--move-namespace",
            "app",
            "--unarchive",
            "--plural",
        ]
    )
    assert rc == 0, p
    assert p["key"]["name"] == "hl::renamed" and p["key"]["namespace"] == "app", p
    assert p["key"]["is_plural"] and not p["key"]["is_archived"], p

    rc, p = _run_json(
        [
            "keys",
            "delete",
            "--project",
            str(pid),
            "--name",
            "hl::renamed",
            "--namespace",
            "app",
            "--yes",
        ]
    )
    assert rc == 0, p
    # a deleted key is invisible by default and findable with --include-deleted
    rc, p = _run_json(["keys", "list", "--project", str(pid)])
    assert rc == 0 and "hl::renamed" not in {k["name"] for k in p["keys"]}, p
    rc, p = _run_json(["keys", "list", "--project", str(pid), "--include-deleted"])
    assert rc == 0, p
    row = next(k for k in p["keys"] if k["name"] == "hl::renamed")
    assert row["deleted"], row

    rc, p = _run_json(
        ["keys", "restore", "--project", str(pid), "--name", "hl::renamed", "--namespace", "app"]
    )
    assert rc == 0 and not p["key"]["deleted"], p
    rc, p = _run_json(
        ["keys", "restore", "--project", str(pid), "--name", "hl::renamed", "--namespace", "app"]
    )
    assert rc != 0 and "not deleted" in (p.get("error") or ""), p

    # --- REMOTE (the same two verbs, through the real /api) -------------------
    _auth(True)
    try:
        client = TestClient(app)
        tok = _key_for("keysurf-hlremote@x.com", scopes="api keys")
        _remote._set_transport(_mock_transport(client))
        try:

            def remote(*argv):
                return _run_json(["--remote", "http://127.0.0.1", "--token", tok, *argv])

            rc, p = remote(
                "keys",
                "edit",
                "--project",
                str(pid),
                "--name",
                "hl::renamed",
                "--namespace",
                "app",
                "--description",
                "via remote",
                "--single",
                "--force",
            )
            assert rc == 0, p
            assert p["key"]["description"] == "via remote", p
            assert not p["key"]["is_plural"], p

            rc, p = remote(
                "keys",
                "delete",
                "--project",
                str(pid),
                "--name",
                "hl::renamed",
                "--namespace",
                "app",
                "--yes",
            )
            assert rc == 0, p
            rc, p = remote("keys", "list", "--project", str(pid), "--include-deleted")
            assert rc == 0 and any(
                k["name"] == "hl::renamed" and k["deleted"] for k in p["keys"]
            ), p
            rc, p = remote(
                "keys",
                "restore",
                "--project",
                str(pid),
                "--name",
                "hl::renamed",
                "--namespace",
                "app",
            )
            assert rc == 0 and not p["key"]["deleted"], p
        finally:
            _remote._set_transport(None)
    finally:
        _auth(False)
    with Session(engine) as s:
        key = keys_svc.get_key(s, pid, "hl::renamed", "app")
        assert key is not None and key.deleted_at is None, key
        assert key.description == "via remote", key.description
    print(
        "  [J] `keys restore` and `keys edit` work locally AND over --remote, and "
        "`keys list --include-deleted` is how you find a key to restore OK"
    )


def test_a_patch_that_fails_applies_nothing():
    """[J] B-6. Each of the five service seams a key edit can reach COMMITS, so
    applying them in sequence had NO all-or-nothing property: a PATCH carrying a good
    description and a colliding name answered 400 with the description already durable
    AND an `updated` entry already on the change feed. A consumer that reads a refusal
    as "nothing happened" then drifts from the server permanently."""
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust, targets="de-DE")
        victim = _add_key(pid, "patch::victim", base="Victim")
        _add_key(pid, "patch::taken", base="Taken")
        tok = _key_for("keysurf-patch@x.com", scopes="api keys")
        client = TestClient(app)
        head = {"X-Loc-Token": tok}
        with Session(engine) as s:
            before = keys_svc.changes_since(s, pid, 0, 500)["next_cursor"]

        r = client.patch(
            "/api/keys/%d" % victim,  # noqa: UP031
            headers=head,
            json={"description": "landed anyway", "name": "patch::taken"},
        )
        assert r.status_code == 400, (r.status_code, r.text[:300])
        assert "the key name 'patch::taken' is not available in the default namespace." in r.text, (
            r.text[:300]
        )

        with Session(engine) as s:
            key = s.get(StringKey, victim)
            s.refresh(key)
            assert key.description == "", (
                "a REFUSED patch left its description behind",
                key.description,
            )
            assert key.name == "patch::victim", key.name
            after = keys_svc.changes_since(s, pid, before, 500)
            assert after["entries"] == [], (
                "a refused patch appended change-feed entries, so a consumer now "
                "believes in an edit the server refused",
                after["entries"],
            )

        # ...and a demotion that would destroy translated forms is refused up front
        plural = _add_key(pid, "patch::plural", base="One", plural=True)
        with Session(engine) as s:
            keys_svc.set_value(
                s,
                s.get(StringKey, plural),
                "de-DE",
                "Eins",
                origin=keys_svc.ORIGIN_HUMAN,
                plural_category="one",
                actor="fixture",
            )
        r = client.patch(
            "/api/keys/%d" % plural,  # noqa: UP031
            headers=head,
            json={"description": "also landed anyway", "is_plural": False},
        )
        assert r.status_code == 400 and "plural" in r.text, (r.status_code, r.text[:300])
        with Session(engine) as s:
            key = s.get(StringKey, plural)
            assert key.is_plural and key.description == "", (
                "the refused demotion still wrote its metadata",
                key.description,
            )
        # the same edit WITH force lands whole, so this is a guard, not a dead route
        r = client.patch(
            "/api/keys/%d" % plural,  # noqa: UP031
            headers=head,
            json={"description": "deliberate", "is_plural": False, "force": True},
        )
        assert r.status_code == 200, r.text[:300]
        assert r.json()["key"]["description"] == "deliberate", r.text[:300]
        assert not r.json()["key"]["is_plural"], r.text[:300]
    finally:
        _auth(False)
    print(
        "  [J] a PATCH whose identity/plural check fails applies NOTHING and "
        "appends no change-feed entry; the same edit with --force lands whole OK"
    )


def test_a_key_edit_walks_through_no_intermediate_identity():
    """[J] W2/W3 (re-review). B-6's fix validated the FINAL identity tuple and then
    applied it through `update_key` -> `rename_key` -> `move_namespace`, each of which
    COMMITS -- so the edit still walked through INTERMEDIATE tuples nobody had checked,
    and the reviewer reproduced all three failure shapes:

      (1) `PATCH {name: bbb, namespace: g2, description: PATCHED}` on a key in `g1`
          with `(g1, bbb)` ALREADY TAKEN. The end state `(g2, bbb)` is free, so this
          is a LEGAL edit -- but the rename ran first, inside `g1`, and it answered
          400 NAMING A NAMESPACE THE CALLER NEVER MENTIONED, with the description
          already durable and an `updated` entry already on the feed.
      (2) The same shape where `context` moves: `update_key` assigned it first and the
          partial unique index answered with a raw IntegrityError -- HTTP 500.
      (3) A plain context edit through the web grid's metadata drawer
          (`POST /projects/{pid}/keys/{key_id}/meta`), same 500, no rename involved.

    Validating the intermediate ORDERS is provably insufficient (there are edits with
    no legal order), so the fix removes the intermediate states: one transaction, one
    commit, one `updated` entry."""
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust, targets="de-DE")
        tok = _key_for("keysurf-onetx@x.com", scopes="api keys")
        client = TestClient(app)
        head = {"X-Loc-Token": tok}

        def _mk(name, ns="", ctx=""):
            with Session(engine) as s:
                k = keys_svc.create_key(
                    s,
                    s.get(Project, pid),
                    name,
                    namespace=ns,
                    context=ctx,
                    base_value="v",
                    actor="fixture",
                )
                return k.id

        # (1) THE EXACT SHAPE. Mover lives in g1; (g1, bbb) is taken; (g2, bbb) free.
        mover = _mk("aaa", ns="g1")
        _mk("bbb", ns="g1")
        with Session(engine) as s:
            cur = keys_svc.changes_since(s, pid, 0, 1000)["next_cursor"]
        r = client.patch(
            "/api/keys/%d" % mover,  # noqa: UP031
            headers=head,
            json={"name": "bbb", "namespace": "g2", "description": "PATCHED"},
        )
        assert r.status_code == 200, (
            f"the legal end-state edit was refused by an intermediate tuple: {r.text[:400]}"
        )
        body = r.json()["key"]
        assert (body["name"], body["namespace"], body["description"]) == ("bbb", "g2", "PATCHED"), (
            body
        )
        with Session(engine) as s:
            entries = keys_svc.changes_since(s, pid, cur, 1000)["entries"]
            ups = [e for e in entries if e["key_id"] == mover and e["op"] == "updated"]
            assert len(ups) == 1, ("a single edit must mint exactly ONE `updated` entry", entries)
            # ...and the value rows really followed the key into g2's document.
            key = s.get(StringKey, mover)
            target = keys_svc.internal_document(s, s.get(Project, pid), "g2", create=False)
            assert target is not None, "the namespace document was never created"
            assert {sg.document_id for sg in keys_svc.values_for(s, key)} == {target.id}, (
                "the value rows did not move with the key"
            )

        # (2) THE CONTEXT VARIANT, which used to be a 500 rather than any kind of
        #     refusal: the END state `(h1, ddd, X)` is FREE, but `update_key` assigned
        #     `context` first, producing the INTERMEDIATE tuple `(h1, ccc, X)` -- which
        #     IS taken -- and the partial unique index answered with a raw
        #     IntegrityError straight out of the route.
        mover2 = _mk("ccc", ns="h1")
        _mk("ccc", ns="h1", ctx="X")  # the intermediate tuple update_key assigned
        with Session(engine) as s:
            cur = keys_svc.changes_since(s, pid, 0, 1000)["next_cursor"]
        r = client.patch(
            "/api/keys/%d" % mover2,  # noqa: UP031
            headers=head,
            json={"name": "ddd", "context": "X", "description": "PATCHED2"},
        )
        assert r.status_code == 200, (
            "the context+name edit must be a clean answer, never a 500 from the unique index",
            r.status_code,
            r.text[:400],
        )
        body = r.json()["key"]
        assert (body["name"], body["context"], body["description"]) == ("ddd", "X", "PATCHED2"), (
            body
        )
        with Session(engine) as s:
            ups = [
                e
                for e in keys_svc.changes_since(s, pid, cur, 1000)["entries"]
                if e["key_id"] == mover2 and e["op"] == "updated"
            ]
            assert len(ups) == 1, ups

        # ...and when the END state is taken too it is a CLEAN 400 that persists
        #    nothing and names the tuple THE CALLER ASKED FOR -- not a waypoint.
        victim = _mk("eee", ns="h2")
        _mk("fff", ns="h2", ctx="Y")  # the END state
        _mk("eee", ns="h2", ctx="Y")  # the INTERMEDIATE state
        with Session(engine) as s:
            cur = keys_svc.changes_since(s, pid, 0, 1000)["next_cursor"]
        r = client.patch(
            "/api/keys/%d" % victim,  # noqa: UP031
            headers=head,
            json={"name": "fff", "context": "Y", "description": "MUST NOT LAND"},
        )
        assert r.status_code == 400, (r.status_code, r.text[:400])
        assert "the key name 'fff' is not available in namespace 'h2' (context 'Y')." in r.text, (
            r.text[:300]
        )
        assert "'fff'" in r.text and "h2" in r.text and "'Y'" in r.text, r.text[:300]
        with Session(engine) as s:
            key = s.get(StringKey, victim)
            assert (key.name, key.context, key.description) == ("eee", "", ""), (
                "a refused edit left state behind",
                (key.name, key.context, key.description),
            )
            assert keys_svc.changes_since(s, pid, cur, 1000)["entries"] == [], (
                "a refused edit appended a change-feed entry"
            )

        # (3) W3: the web grid's metadata drawer, a PLAIN context edit onto a taken
        #     tuple -- no rename anywhere near it.
        admin = _login("keysurf-onetx-admin@x.com")
        plain = _mk("ggg", ns="k1")
        _mk("ggg", ns="k1", ctx="TAKEN")
        r = admin.post(
            "/projects/%d/keys/%d/meta" % (pid, plain),  # noqa: UP031
            data={"description": "d", "context": "TAKEN", "tags": "", "char_limit": ""},
            follow_redirects=False,
        )
        assert r.status_code in (303, 400), (
            "a context collision through the metadata drawer must not be a 500",
            r.status_code,
            r.text[:300],
        )
        assert r.status_code != 500, r.status_code
        with Session(engine) as s:
            key = s.get(StringKey, plain)
            assert key.context == "", ("the colliding context was persisted anyway", key.context)
            assert key.description == "", (
                "the drawer's other field landed on a refused save",
                key.description,
            )
        # ...and a FREE context still saves through the same drawer.
        r = admin.post(
            "/projects/%d/keys/%d/meta" % (pid, plain),  # noqa: UP031
            data={"description": "d", "context": "FREE", "tags": "", "char_limit": ""},
            follow_redirects=False,
        )
        assert r.status_code == 303, (r.status_code, r.text[:300])
        with Session(engine) as s:
            key = s.get(StringKey, plain)
            assert (key.context, key.description) == ("FREE", "d"), (key.context, key.description)
    finally:
        _auth(False)
    print(
        "  [J] a key edit is ONE transaction over the whole identity tuple: the "
        "legal (rename + move) edit lands with exactly one `updated` entry and its "
        "value rows, a taken end state is a clean 400 naming what the caller asked "
        "for with nothing persisted, and a colliding context through the grid's "
        "metadata drawer is a refusal instead of a 500 OK"
    )


def test_destructive_key_ops_are_audited():
    """[J] B-9. Delete, restore, import and the auto-unverify toggle are the four key
    operations an incident asks "who did that, and when" about -- a cleanup import can
    retire a whole catalogue and the toggle decides whether a source edit can silently
    ship a stale translation. Written at the SERVICE seam
    (`content.delete_document`'s precedent) so all three surfaces inherit them."""
    from app.models import AuditLog

    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    _add_key(pid, "audit::probe", base="Hello")

    src = os.path.join(_TMP, "audit-%d.json" % _uniq())  # noqa: UP031
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(_json.dumps({"audit::probe": "Hello", "audit::extra": "Extra"}))
    rc, _p = _run_json(
        ["keys", "import", "--project", str(pid), "--file", src, "--namespace", "common"]
    )
    assert rc == 0
    rc, _p = _run_json(["keys", "delete", "--project", str(pid), "--name", "audit::probe", "--yes"])
    assert rc == 0
    rc, _p = _run_json(["keys", "restore", "--project", str(pid), "--name", "audit::probe"])
    assert rc == 0
    with Session(engine) as s:
        found = {
            r.action: r for r in s.exec(select(AuditLog)).all() if r.action.startswith("keys.")
        }
    for action in ("keys.import", "keys.delete", "keys.restore"):
        assert action in found, (action, sorted(found))

    with Session(engine) as s:
        keys_svc.set_autounverify(s.get(Project, pid), False, actor="audit-test")
    with Session(engine) as s:
        found = {
            r.action: r for r in s.exec(select(AuditLog)).all() if r.action.startswith("keys.")
        }
    assert "keys.autounverify" in found, sorted(found)
    assert "mode=merge" in found["keys.import"].detail, found["keys.import"].detail
    assert "created" in found["keys.import"].detail, found["keys.import"].detail
    assert "audit::probe" in found["keys.delete"].target, found["keys.delete"].target
    assert "off" in found["keys.autounverify"].detail, found["keys.autounverify"].detail

    # a CLEANUP import writes ONE row carrying the deleted COUNT, not one per key.
    with Session(engine) as s:
        before = len(s.exec(select(AuditLog)).all())
    sweep = os.path.join(_TMP, "audit-sweep-%d.json" % _uniq())  # noqa: UP031
    with open(sweep, "w", encoding="utf-8") as fh:
        fh.write(_json.dumps({"audit::probe": "Hello"}))
    rc, p = _run_json(
        [
            "keys",
            "import",
            "--project",
            str(pid),
            "--file",
            sweep,
            "--namespace",
            "common",
            "--cleanup",
            "--yes",
        ]
    )
    assert rc == 0 and p["deleted"] == 1, p
    with Session(engine) as s:
        added = [r for r in s.exec(select(AuditLog)).all()][before:]  # noqa: C416
    assert [r.action for r in added] == ["keys.import"], (
        "a cleanup import must write ONE row carrying its deleted count, not one per swept key",
        [r.action for r in added],
    )
    assert "1 deleted" in added[0].detail and "cleanup" in added[0].detail, added[0].detail
    print(
        "  [J] keys.delete / keys.restore / keys.import / keys.autounverify each "
        "leave an audit row, and a cleanup import leaves exactly one OK"
    )


def test_locale_changes_are_attributed_on_every_surface():
    """[J] B-5. The add/remove-locale seams mint change-feed entries -- a whole
    locale's worth of `value`/`created` rows on the way in, and its tombstones on the
    way out -- and every one of them named NOBODY (`actor: ""`).

    The cause is the one the wave itself wrote down (re-review R5): the ORM listener
    and `log_bulk_value_deletions` read `keys.current_actor()`, and FastAPI runs a
    sync route handler on anyio's worker POOL, where that is "" at best and another
    request's leftover at worst. The removal half is the costly one -- a tombstone is
    the single event a delta consumer cannot recover from by re-reading, so "who
    deleted every French value" had no answer at all."""
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust, targets="de-DE")
        _add_key(pid, "actor::locale", base="Hello")
        admin = _login("keysurf-locale@x.com")
        # add-locale is a PROJECT route, so it wants the docs scope, not the keys one
        # -- which is exactly why the entries it mints had no actor: nothing on that
        # path knows or cares about keys.
        tok = _key_for("keysurf-localeapi@x.com", scopes="api docs")
        client = TestClient(app)

        with Session(engine) as s:
            before = keys_svc.changes_since(s, pid, 0, 500)["next_cursor"]
        r = admin.post(
            "/projects/%d/add-locale" % pid,  # noqa: UP031
            data={"locale": "fr-FR"},
            follow_redirects=False,  # noqa: RUF100, UP031
        )
        assert r.status_code == 303, (r.status_code, r.text[:200])
        with Session(engine) as s:
            added = [
                e
                for e in keys_svc.changes_since(s, pid, before, 500)["entries"]
                if e["entity"] == "value" and e["target_locale"] == "fr-FR"
            ]
            assert added, "adding a locale minted no value entries at all"
            assert all(e["actor"] == "keysurf-locale@x.com" for e in added), (
                "an added locale's value entries are UNATTRIBUTED",
                [(e["op"], e["actor"]) for e in added],
            )
            before = keys_svc.changes_since(s, pid, 0, 500)["next_cursor"]

        # ...and the REMOVAL half, through /api (the twin that also had no actor).
        csrf_for(admin)
        r = admin.post(
            "/projects/%d/remove-locale" % pid,  # noqa: UP031
            data={"locale": "fr-FR", "confirm_locale": "fr-FR"},
            follow_redirects=False,
        )
        assert r.status_code == 303, (r.status_code, r.text[:300])
        with Session(engine) as s:
            gone = [
                e
                for e in keys_svc.changes_since(s, pid, before, 500)["entries"]
                if e["op"] == "deleted" and e["target_locale"] == "fr-FR"
            ]
            assert gone, "removing a locale minted no tombstones at all"
            assert all(e["actor"] == "keysurf-locale@x.com" for e in gone), (
                "a removed locale's TOMBSTONES are unattributed -- the one event a "
                "consumer cannot recover from by re-reading",
                [(e["op"], e["actor"]) for e in gone],
            )
            before = keys_svc.changes_since(s, pid, 0, 500)["next_cursor"]

        # the /api add-locale twin carries its token's actor too
        r = client.post(
            "/api/projects/%d/add-locale" % pid,  # noqa: UP031
            headers={"X-Loc-Token": tok},
            json={"locale": "fr-FR"},
        )
        assert r.status_code == 200, (r.status_code, r.text[:300])
        with Session(engine) as s:
            added = [
                e
                for e in keys_svc.changes_since(s, pid, before, 500)["entries"]
                if e["entity"] == "value" and e["target_locale"] == "fr-FR"
            ]
            assert added and all(e["actor"] == "keysurf-localeapi@x.com" for e in added), (
                "the /api add-locale twin is unattributed",
                [(e["op"], e["actor"]) for e in added],
            )
    finally:
        _auth(False)
    print(
        "  [J] add-locale and remove-locale attribute every value entry and every "
        "tombstone they mint, on the web AND /api surfaces OK"
    )


def test_api_list_excludes_archived_by_default():
    """[J] B-10a. `5149cc5`'s whole point, and nothing tested it: ARCHIVED means
    "retired from the product, kept for the record", and the web grid's checkbox,
    `keys list --archived` and `export_locale` all default to excluding them. A
    surface that defaulted the other way would make the same question give three
    answers -- and the /api list is the one a CI job asks."""
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust, targets="de-DE")
        _add_key(pid, "arch::live", base="Live")
        retired = _add_key(pid, "arch::retired", base="Retired")
        with Session(engine) as s:
            keys_svc.update_key(s, s.get(StringKey, retired), is_archived=True, actor="fixture")
        tok = _key_for("keysurf-archived@x.com", scopes="api keys")
        client = TestClient(app)
        head = {"X-Loc-Token": tok}

        r = client.get("/api/projects/%d/keys" % pid, headers=head)  # noqa: UP031
        assert r.status_code == 200, r.text[:200]
        assert {k["name"] for k in r.json()["keys"]} == {"arch::live"}, (
            "the /api key list must EXCLUDE archived keys by default, like the grid, "
            "the CLI and export_locale",
            r.json()["keys"],
        )
        r = client.get("/api/projects/%d/keys?archived=1" % pid, headers=head)  # noqa: UP031
        assert {k["name"] for k in r.json()["keys"]} == {"arch::live", "arch::retired"}, r.text[
            :300
        ]
    finally:
        _auth(False)
    print(
        "  [J] the /api key list excludes archived keys by default and includes "
        "them on ?archived=1 OK"
    )


def test_adding_a_locale_seeds_no_junk_into_a_key_document():
    """[J] B-10b. `content._seed_locale_segments` CLONES another locale's segments
    positionally when a locale is added -- which for an internal key document minted
    key-LESS rows inside a TRANSLATABLE document: the change feed cannot describe them
    (they carry no key_id) and a run would spend budget translating them. The skip
    shipped with no regression test at all; key projects go through
    `_materialize_key_values`, which asks CLDR what rows the locale needs."""
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    kid = _add_key(pid, "seed::probe", base="Hello")
    with Session(engine) as s:
        assert content.add_target_locale(s, pid, "ru-RU")
    with Session(engine) as s:
        from app.models import Segment

        docs = keys_svc.key_documents(s, pid)
        rows = s.exec(select(Segment).where(Segment.document_id.in_([d.id for d in docs]))).all()
        assert rows, "the fixture must have value rows"
        orphans = [r for r in rows if not r.key_id]
        assert not orphans, (
            "adding a locale cloned key-LESS segments into an internal key document "
            "-- the feed cannot describe them and a run would translate them",
            [(r.id, r.target_locale, r.source_text) for r in orphans],
        )
        ru = [r for r in rows if r.target_locale == "ru-RU"]
        # ru has FOUR CLDR forms, so a positional clone of de-DE's single row would
        # also be the wrong SHAPE, not merely unattributed.
        assert {r.plural_category for r in ru} == {None}, ru
        assert len(ru) == 1 and ru[0].key_id == kid, ru
        assert ru[0].source_text == "Hello", ru[0].source_text
    print(
        "  [J] adding a locale to a key project materializes CLDR-correct value "
        "rows and seeds NO key-less segments into the internal documents OK"
    )


def test_the_autounverify_route():
    """[J] B-10d. The per-project auto-unverify override (spec section 8) decides
    whether a source edit can silently ship a stale translation -- the route that
    writes it had no test, so "off" could have meant nothing at all."""
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust, targets="de-DE")
        kid = _add_key(pid, "unverify::probe", base="Hello")
        admin = _login("keysurf-unverify@x.com")
        with Session(engine) as s:
            keys_svc.set_value(
                s,
                s.get(StringKey, kid),
                "de-DE",
                "Hallo",
                origin=keys_svc.ORIGIN_HUMAN,
                actor="fixture",
            )

        r = admin.post(
            "/projects/%d/keys/autounverify" % pid,  # noqa: UP031
            data={"on": "0"},
            follow_redirects=False,  # noqa: RUF100, UP031
        )
        assert r.status_code == 303, (r.status_code, r.text[:200])
        with Session(engine) as s:
            assert not keys_svc.autounverify_enabled(s.get(Project, pid))
            # ...and it really governs the behaviour, not just the stored string
            keys_svc.set_base_value(s, s.get(StringKey, kid), "Hello again", actor="fixture")
            seg = keys_svc.values_for(s, s.get(StringKey, kid))[0]
            assert seg.status != "needs_review", (
                "auto-unverify is OFF, so a base change must not flag anything",
                seg.status,
            )

        r = admin.post(
            "/projects/%d/keys/autounverify" % pid,  # noqa: UP031
            data={"on": "1"},
            follow_redirects=False,  # noqa: RUF100, UP031
        )
        assert r.status_code == 303, r.status_code
        with Session(engine) as s:
            assert keys_svc.autounverify_enabled(s.get(Project, pid))
            keys_svc.set_base_value(s, s.get(StringKey, kid), "Hello once more", actor="fixture")
            seg = keys_svc.values_for(s, s.get(StringKey, kid))[0]
            assert seg.status == "needs_review", (
                "auto-unverify is ON, so a base change must flag the translation",
                seg.status,
            )

        r = admin.post(
            "/projects/%d/keys/autounverify" % pid,  # noqa: UP031
            data={"on": "inherit"},
            follow_redirects=False,  # noqa: RUF100, UP031
        )
        assert r.status_code == 303, r.status_code
        with Session(engine) as s:
            from app.models import Setting

            row = s.exec(select(Setting).where(Setting.key == "key_autounverify:%d" % pid)).first()  # noqa: UP031
            assert row is not None and row.value == "", (
                "'inherit' must CLEAR the override (a blank row), not write a value",
                row.value if row else None,
            )
    finally:
        _auth(False)
    print(
        "  [J] POST .../keys/autounverify stores on / off / inherit, and the stored "
        "value really governs whether a base change flags a translation OK"
    )


def test_restore_fills_locales_added_while_tombstoned():
    """[J] B-15. Nothing materializes value rows for a DELETED key --
    `content._materialize_key_values` walks `list_keys`, which excludes it -- so a
    locale added while a key was in the trash came back with a GAP: the grid rendered
    a dash and no input for that locale, and only a base edit could ever fill it."""
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    kid = _add_key(pid, "gap::probe", base="Hello")
    with Session(engine) as s:
        keys_svc.delete_key(s, s.get(StringKey, kid), actor="test")
    with Session(engine) as s:
        assert content.add_target_locale(s, pid, "fr-FR")
    with Session(engine) as s:
        key = s.get(StringKey, kid)
        assert {v.target_locale for v in keys_svc.values_for(s, key)} == {"de-DE"}, (
            "the fixture must reproduce the GAP -- a tombstoned key gets no rows"
        )
        before = keys_svc.changes_since(s, pid, 0, 500)["next_cursor"]
        keys_svc.restore_key(s, key, actor="test")
    with Session(engine) as s:
        key = s.get(StringKey, kid)
        rows = keys_svc.values_for(s, key)
        assert {v.target_locale for v in rows} == {"de-DE", "fr-FR"}, (
            "a restored key still has no row for the locale added while it was tombstoned",
            [(v.target_locale, v.id) for v in rows],
        )
        fr = next(v for v in rows if v.target_locale == "fr-FR")
        assert fr.source_text == "Hello", (
            "the new locale's row must carry the key's base value",
            fr.source_text,
        )
        # every row the consumer gets back is announced EXACTLY ONCE
        entries = [
            e
            for e in keys_svc.changes_since(s, pid, before, 500)["entries"]
            if e["entity"] == "value"
        ]
        seen = [(e["target_locale"], e["plural_category"]) for e in entries]
        assert sorted(seen) == [("de-DE", ""), ("fr-FR", "")], (
            "restore must announce each value row once -- no gaps, no duplicates",
            seen,
        )
    print(
        "  [J] restore materializes the rows for a locale added while the key was "
        "tombstoned, and announces every row exactly once OK"
    )


# ============================================================================
# [K] the fix round's small-surface cells
# ============================================================================
def test_a_key_project_shows_real_progress():
    """[K] B-12. A key project has no listable documents by construction, so the
    /projects rollup read zero segments and rendered a progress bar stuck at 0%
    forever, however completely translated the catalogue was. An empty bar that can
    never move is worse than no bar."""
    from app.web.queries import _project_rows

    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    a = _add_key(pid, "prog::a", base="A")
    b = _add_key(pid, "prog::b", base="B")
    with Session(engine) as s:
        for kid in (a, b):
            keys_svc.set_value(
                s,
                s.get(StringKey, kid),
                "de-DE",
                "x",
                origin=keys_svc.ORIGIN_HUMAN,
                status="approved",
                actor="fixture",
            )
        project = s.get(Project, pid)
        row = _project_rows(s, [project], {})[0]
        assert row["approved_pct"] == 100, (
            "a fully approved key catalogue must not render a 0% bar",
            row["approved_pct"],
        )
        assert row["docs"] == 0, (
            "the document COUNT stays honest (a key project has none)",
            row["docs"],
        )

        # ...and a DELETED key's parked values must not hold the bar down forever.
        c = keys_svc.create_key(s, project, "prog::c", base_value="C", actor="fix")
        keys_svc.delete_key(s, c, actor="fixture")
    with Session(engine) as s:
        row = _project_rows(s, [s.get(Project, pid)], {})[0]
        assert row["approved_pct"] == 100, (
            "a soft-deleted key's parked value rows are counted in the progress bar, "
            "so a cleanup import would permanently depress it",
            row["approved_pct"],
        )
    print(
        "  [K] a key project's /projects row shows real approved-value progress, "
        "counts no documents, and ignores a deleted key's parked rows OK"
    )


def test_project_export_on_a_key_project_names_the_key_surface():
    """[K] B-13. `delivery.export_project` answered "project has no documents to
    export" -- true, useless, and reads as a fault on a project type that has none by
    design and whose deliverable is a per-locale key file."""
    from app.services import delivery

    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    _add_key(pid, "exp::probe", base="Hello")
    with Session(engine) as s:
        try:
            delivery.export_project(s, pid)
            raise AssertionError("a key project has no document bundle to export")
        except ValueError as exc:
            msg = str(exc)
    assert "keys export" in msg and "key grid" in msg, msg
    assert "no documents to export" not in msg, msg

    client = TestClient(app)
    r = client.get("/projects/%d/export" % pid, follow_redirects=False)  # noqa: UP031
    assert r.status_code == 303, r.status_code
    loc = r.headers.get("location") or ""
    assert "keys+export" in loc or "keys%20export" in loc, loc
    print(
        "  [K] project export on a key project names `keys export` and the grid's "
        "Export, on the service seam AND the web route OK"
    )


def test_a_viewer_may_export_from_the_grid():
    """[K] B-14. The Export card sat inside the grid's `can_write` gate, so a viewer
    saw no way to download a build file -- while the export ROUTE happily answered 200
    for them. Downloading is a read; the gate was describing the wrong thing."""
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust, targets="de-DE")
        _add_key(pid, "vexp::probe", base="Hello")
        viewer = security.create_user(
            "keysurf-vexport@x.com", "keysurf-vexport@x.com", Role.VIEWER, PW
        )
        with Session(engine) as s:
            s.add(ProjectMember(project_id=pid, user_id=viewer.id))
            s.commit()
        client = _login("keysurf-vexport2@x.com", role=Role.VIEWER)
        # (the helper creates + logs in its own user; make it a member too)
        with Session(engine) as s:
            from app.models import User

            u = s.exec(select(User).where(User.email == "keysurf-vexport2@x.com")).first()
            s.add(ProjectMember(project_id=pid, user_id=u.id))
            s.commit()

        html = client.get("/projects/%d/keys" % pid).text  # noqa: UP031
        assert "/keys/export" in html, (
            "a viewer is offered no way to download a build file, while the export "
            "route answers 200 for them"
        )
        assert "/keys/import" not in html, "...but Import is a WRITE and must still be gated"
        r = client.get("/projects/%d/keys/export?locale=de-DE" % pid, follow_redirects=False)  # noqa: UP031
        assert r.status_code == 200, (r.status_code, r.text[:200])
    finally:
        _auth(False)
    print(
        "  [K] a VIEWER is offered the Export card (and the route serves them) "
        "while Import stays behind the write gate OK"
    )


def test_one_flag_name_and_a_refused_limit():
    """[K] B-18. `archived` on /api and the CLI, `include_archived` on the web export
    form: the same question with two names, so a caller copying one into the other
    silently got the DEFAULT answer. And `limit=-5` became a page of ONE -- clamping
    is right for a service seam that must not crash on junk, and wrong for an API that
    can just say no."""
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust, targets="de-DE")
        _add_key(pid, "flaglive", base="Live")
        retired = _add_key(pid, "flagretired", base="Retired")
        with Session(engine) as s:
            keys_svc.update_key(s, s.get(StringKey, retired), is_archived=True, actor="fixture")
        admin = _login("keysurf-flag@x.com")
        tok = _key_for("keysurf-flagapi@x.com", scopes="api keys")
        client = TestClient(app)
        head = {"X-Loc-Token": tok}

        # the grid's export form and the /api export answer the SAME flag name
        html = admin.get("/projects/%d/keys" % pid).text  # noqa: UP031
        assert 'name="archived"' in html, (
            "the grid's export form must post `archived`, the name /api and the CLI use"
        )
        assert 'name="include_archived"' not in html, html[:200]
        for url in ("/projects/%d/keys/export?locale=de-DE&archived=1" % pid,):  # noqa: UP031
            body = admin.get(url).text
            assert "flagretired" in body, ("the web export ignored `archived`", url)
        body = client.get(
            "/api/projects/%d/keys/export?locale=de-DE&archived=1" % pid,  # noqa: UP031
            headers=head,  # noqa: RUF100, UP031
        ).text
        assert "flagretired" in body, body[:200]

        # a nonsense limit is a refusal, on both paged endpoints
        for path in (
            "/api/projects/%d/keys?limit=-5" % pid,  # noqa: UP031
            "/api/projects/%d/changes?limit=-5" % pid,  # noqa: UP031
        ):
            r = client.get(path, headers=head)
            assert r.status_code == 422, (path, r.status_code, r.text[:200])
            assert "limit must be" in r.text, (path, r.text[:200])
        # ...and the LIST also refuses one above its ceiling (it has no "give me all")
        r = client.get("/api/projects/%d/keys?limit=100000" % pid, headers=head)  # noqa: UP031
        assert r.status_code == 422 and "between 1 and" in r.text, r.text[:200]
        # ...and 0 still means "the server default" on /changes only
        assert client.get("/api/projects/%d/changes?limit=0" % pid, headers=head).status_code == 200  # noqa: UP031
        assert client.get("/api/projects/%d/keys?limit=0" % pid, headers=head).status_code == 422  # noqa: UP031
    finally:
        _auth(False)
    print(
        "  [K] `archived` is one name across the grid form, /api and the CLI; a "
        "limit outside [1, max] is a 422 instead of a silent page of one OK"
    )


def test_changes_echoes_the_effective_limit():
    """[K] A-F10. `changes_since` clamps to MAX_DELTA_LIMIT, so a consumer asking for
    5,000 got 1,000 rows and `has_more: true` with no way to tell whether the cap was
    its own request or the server's -- i.e. it could not learn its real page size,
    which is the one number a paging client tunes."""
    _auth(True)
    try:
        cust = _mk_customer()
        pid = _mk_key_project(cust, targets="de-DE")
        _add_key(pid, "echo::probe", base="Hello")
        tok = _key_for("keysurf-echo@x.com", scopes="api keys")
        client = TestClient(app)
        head = {"X-Loc-Token": tok}

        body = client.get("/api/projects/%d/changes" % pid, headers=head).json()  # noqa: UP031
        assert body["limit"] == keys_svc.DEFAULT_DELTA_LIMIT, body
        assert body["max_limit"] == keys_svc.MAX_DELTA_LIMIT, body
        body = client.get("/api/projects/%d/changes?limit=2" % pid, headers=head).json()  # noqa: UP031
        assert body["limit"] == 2, body
        # ABOVE the max the server still CLAMPS (a pinned behaviour), and the echo is
        # the whole point: the consumer can now SEE that it was capped.
        r = client.get(
            "/api/projects/%d/changes?limit=%d" % (pid, keys_svc.MAX_DELTA_LIMIT + 1),  # noqa: UP031
            headers=head,  # noqa: RUF100, UP031
        )
        assert r.status_code == 200, (r.status_code, r.text[:200])
        assert r.json()["limit"] == keys_svc.MAX_DELTA_LIMIT, r.text[:300]
    finally:
        _auth(False)
    print(
        "  [K] /changes echoes the EFFECTIVE limit and the server's max, and "
        "CLAMPS one above it (200, not a refusal -- the deliberate asymmetry with "
        "the key list, see the route) OK"
    )


def test_the_switch_off_leaves_no_dead_links_and_names_no_type():
    """[K] A-F8 + A-F9. With the switch off, `/projects/{id}/keys` is a 404 -- but the
    project page and the /projects table still rendered LINKS to it, so a key project
    became a page of dead affordances with a document project's empty upload form
    where its content should be. And the create refusal ENUMERATED `key_based` in its
    "expected one of" list: a kill switch that advertises what it disables, and hands
    a prober the exact string to keep trying."""
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    _add_key(pid, "off::probe", base="Hello")
    client = TestClient(app)
    _switch(False)
    try:
        page = client.get("/projects/%d" % pid).text  # noqa: UP031
        assert "/projects/%d/keys" % pid not in page, (  # noqa: UP031
            "the project page links to a key grid that 404s with the switch off"
        )
        assert "turned off on this install" in page, (
            "...and it must say WHY there is nothing to click, rather than silently "
            "rendering a document project's page"
        )
        assert "Upload a document" not in page, (
            "a key project must never offer a document upload, switch or no switch"
        )
        listing = client.get("/projects").text
        assert "/projects/%d/keys" % pid not in listing, (  # noqa: UP031
            "the /projects Keys badge links to a 404 with the switch off"
        )

        with Session(engine) as s:
            try:
                content.project_create(
                    s,
                    "OffType %d" % _uniq(),  # noqa: UP031
                    source_locale="en-US",
                    target_locales="de-DE",
                    customer_id=str(cust),
                    project_type="key_based",
                )
                raise AssertionError("the switch must refuse a key_based create")
            except content.InvalidProjectTypeError as exc:
                msg = str(exc)
        assert "key_based" in msg.split("expected one of:")[0], msg
        assert "key_based" not in msg.split("expected one of:")[1], (
            "the refusal ENUMERATES the very type the kill switch disables",
            msg,
        )
    finally:
        _switch(True)
    # ...and with the switch back ON the affordance returns, so this is a gate and
    # not a deletion.
    assert "/projects/%d/keys" % pid in client.get("/projects/%d" % pid).text  # noqa: UP031
    with Session(engine) as s:
        assert "key_based" in str(content.InvalidProjectTypeError("nope"))
    print(
        "  [K] with the switch off a key project's page links nowhere and says why, "
        "the /projects badge is gone, and the create refusal no longer enumerates "
        "the disabled type OK"
    )


# ============================================================================
# [L] The live-QA findings, surface half (AUDIT-keys-live-qa-2026-08-03)
# ============================================================================
def _damage(pid, name, namespace=""):
    """Give a key's de-DE value a stray mask token the source never had -- the exact
    leak the live local engine produced and the tag-integrity guard flagged."""
    with Session(engine) as s:
        key = keys_svc.get_key(s, pid, name, namespace)
        keys_svc.set_value(
            s, key, "de-DE", "Einstellungen ⟦x1⟧", origin=keys_svc.ORIGIN_HUMAN, actor="fixture"
        )


def test_an_export_tells_every_surface_what_the_tag_guard_did():
    """LIVE-QA K2 (MEDIUM). `keys export --json` answered `{ok, bytes, path}` and the
    web download answered bytes, so five German strings replaced by ENGLISH shipped
    with nothing said on any surface. The seam had counted them the whole time.

    Asserted on all four: `--json`, human CLI, the browser download and `--remote`."""
    _auth(False)
    cust = _mk_customer()
    pid = _mk_key_project(cust)
    _add_key(pid, "settings", base="Settings", namespace="nav")
    _damage(pid, "settings", "nav")

    # (a) --json: machine-readable counts AND names, which is what a release pipeline
    #     can actually gate on.
    rc, p = _run_json(
        ["keys", "export", "--project", str(pid), "--locale", "de-DE", "--namespace", "nav"]
    )
    assert rc == 0, p
    assert p["substituted"] == 1 and p["substituted_keys"] == ["settings"], p
    assert p["kept_damaged"] == 0, p
    assert "Settings" in p["content"] and "⟦" not in p["content"], p

    # (b) human mode: the warning names the key, on STDERR so a `> file.json` redirect
    #     still gets clean bytes.
    rc, out = _run_both(
        [
            "keys",
            "export",
            "--project",
            str(pid),
            "--locale",
            "de-DE",
            "--namespace",
            "nav",
            "--out",
            os.path.join(_TMP, "k2-%d.json" % _uniq()),  # noqa: UP031
        ]
    )
    assert rc == 0, out
    assert "SOURCE text" in out and "settings" in out, out

    # (c) the TRANSPORT file substitutes nothing and says THAT instead.
    rc, p = _run_json(["keys", "export", "--project", str(pid), "--locale", "de-DE"])
    assert rc == 0 and p["substituted"] == 0, p
    assert p["kept_damaged"] == 1 and p["kept_damaged_keys"] == ["nav::settings"], p

    # (d) the browser download: headers for a machine, and a notice the grid renders
    #     ONCE for a human -- a download response has no page to warn on.
    client = TestClient(app)
    r = client.get("/projects/%d/keys/export?locale=de-DE&namespace=nav" % pid)  # noqa: UP031
    assert r.status_code == 200, r.status_code
    assert r.headers[key_files.HDR_SUBSTITUTED] == "1", dict(r.headers)
    assert r.headers[key_files.HDR_SUBSTITUTED_KEYS] == "settings", dict(r.headers)
    html = client.get("/projects/%d/keys" % pid).text  # noqa: UP031
    assert "Last export:" in html and "settings" in html, "no notice on the grid"
    again = client.get("/projects/%d/keys" % pid).text  # noqa: UP031
    assert "Last export:" not in again, "the notice must be one-shot"

    # (e) --remote prints what local prints, rebuilt from those headers.
    _auth(True)
    try:
        tok = _key_for("keysurf-k2@x.com", scopes="api keys")
        _remote._set_transport(_mock_transport(TestClient(app)))
        try:
            rc, p = _run_json(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    tok,
                    "keys",
                    "export",
                    "--project",
                    str(pid),
                    "--locale",
                    "de-DE",
                    "--namespace",
                    "nav",
                ]
            )
            assert rc == 0, p
            assert p["substituted"] == 1 and p["substituted_keys"] == ["settings"], p
        finally:
            _remote._set_transport(None)
    finally:
        _auth(False)
    print(
        "  [L] an export names the keys the tag guard touched on every surface: "
        "--json, human CLI, the download's headers + a one-shot grid notice, and "
        "--remote OK"
    )


def test_the_withheld_report_reaches_the_cli_json_payload():
    """FINAL ROUND, MUTATION M28. `withheld_filled` / `withheld_filled_keys` are listed
    in SPEC section 4 as a K2 surface and documented in `docs/CLI.md`, and ZEROING BOTH
    IN `_export_payload` PASSED THE WHOLE SUITE: the K12 cell asserts the service seam
    and the download headers, and no test ever read the CLI payload. That is the
    surface a release pipeline gates on.

    A WITHHELD value is empty AND flagged needs-review -- a quarantined refusal, or MT
    the gate declined to commit. `--empty-as base` fills it with English, which is the
    export answering a question the product deliberately refused to answer."""
    _auth(False)
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    _add_key(pid, "start", base="Start now", namespace="cta")
    _add_key(pid, "quiet", base="Quiet", namespace="cta")
    with Session(engine) as s:
        seg = keys_svc.values_for(s, keys_svc.get_key(s, pid, "start", "cta"), "de-DE")[0]
        seg.status = SegStatus.NEEDS_REVIEW  # withheld: flagged, no text
        s.add(seg)
        s.commit()
    rc, p = _run_json(
        [
            "keys",
            "export",
            "--project",
            str(pid),
            "--locale",
            "de-DE",
            "--namespace",
            "cta",
            "--empty-as",
            "base",
        ]
    )
    assert rc == 0, p
    assert p["withheld_filled"] == 1, p
    assert p["withheld_filled_keys"] == ["start"], p
    # ...and the OTHER empty row, which nobody withheld, is filled in silence.
    assert "quiet" not in _json.dumps(p["withheld_filled_keys"]), p
    # The human arm carries the same fact, on stderr.
    rc, out = _run_both(
        [
            "keys",
            "export",
            "--project",
            str(pid),
            "--locale",
            "de-DE",
            "--namespace",
            "cta",
            "--empty-as",
            "base",
            "--out",
            os.path.join(_TMP, "m28-%d.json" % _uniq()),  # noqa: UP031
        ]
    )
    assert rc == 0, out
    assert "WITHHELD" in out and "start" in out, out
    print(
        "  [L] a WITHHELD value filled from source is reported on the CLI --json "
        "payload, not only at the service seam OK"
    )


def test_a_singular_base_on_a_plural_key_says_it_filled_other_only():
    """LIVE-QA K5 (LOW). `--base` fanned one SINGULAR string across every CLDR form,
    while its help said it fills `other` -- so the plural forms' SOURCE text was wrong
    and MT translated a singular sentence into every slot. Behaviour and help agree
    now, and both surfaces SAY which forms still need source text."""
    _auth(False)
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="ru-RU")
    rc, p = _run_json(
        ["keys", "add", "--project", str(pid), "--name", "items", "--plural", "--base", "%d items"]
    )
    assert rc == 0, p
    assert "hint" in p and "'other' form only" in p["hint"], p
    assert "few" in p["hint"] and "many" in p["hint"], p
    rc, out = _run(
        ["keys", "add", "--project", str(pid), "--name", "files", "--plural", "--base", "%d files"]
    )
    assert rc == 0 and "'other' form only" in out, out
    with Session(engine) as s:
        base = keys_svc.base_values(s, keys_svc.get_key(s, pid, "items", ""))
        assert base["other"] == "%d items" and not base["few"], base
    # ...and the web add form says the same thing, in the same words.
    admin = _login("keysurf-k5@x.com")
    r = admin.post(
        "/projects/%d/keys/add" % pid,  # noqa: UP031
        data={
            "name": "folders",
            "is_plural": "1",
            "base_value": "%d folders",
            "csrf_token": csrf_for(admin),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    loc = unquote(r.headers["location"])
    assert "'other' form only" in loc and "still need their own source text" in loc, loc
    print(
        "  [L] a singular --base on a plural key fills 'other' only, and the CLI and "
        "the web grid both say which forms are still without source text OK"
    )


# ============================================================================
# [M] The FIX ROUND over [L] -- the surface half
# ============================================================================
def test_the_first_base_edit_does_not_re_fan_the_singular_string():
    """FIX-ROUND F6. K5 stopped `keys add --plural --base X` fanning one singular
    string across every CLDR form -- and then the FIRST base edit did it anyway, on
    BOTH surfaces, including through the very remediation K5's own new hint recommends
    (`keys set --form ...`).

    Both call sites merge the STORED base map, which holds `""` for the forms K5
    deliberately left empty, and `""` is falsy: `_source_for`'s fallback read a
    named-but-empty form as unnamed and re-fanned `other` across all four. It dragged
    auto-unverify with it, so every translation already in the key was flagged too."""
    _auth(False)
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="ru-RU")

    # (a) the CLI, following the hint K5 prints.
    rc, p = _run_json(
        ["keys", "add", "--project", str(pid), "--name", "items", "--plural", "--base", "%d items"]
    )
    assert rc == 0, p
    with Session(engine) as s:
        key = keys_svc.get_key(s, pid, "items", "")
        keys_svc.set_value(
            s,
            key,
            "ru-RU",
            "shtuka",
            origin=keys_svc.ORIGIN_HUMAN,
            plural_category="one",
            actor="fixture",
        )
    rc, out = _run(
        [
            "keys",
            "set",
            "--project",
            str(pid),
            "--name",
            "items",
            "--locale",
            "en-US",
            "--form",
            "other",
            "--text",
            "%d NEW items",
        ]
    )
    assert rc == 0, out
    with Session(engine) as s:
        key = keys_svc.get_key(s, pid, "items", "")
        base = keys_svc.base_values(s, key)
        assert base["other"] == "%d NEW items", base
        assert base["one"] == "" and base["few"] == "" and base["many"] == "", base
        seg = [x for x in keys_svc.values_for(s, key, "ru-RU") if x.plural_category == "one"][0]  # noqa: RUF015
        assert seg.status != SegStatus.NEEDS_REVIEW, (
            "editing `other` must not flag a translation of `one` for review"
        )

    # (b) the grid's base editor, the same claim.
    admin = _login("keysurf-f6@x.com")
    rc, p = _run_json(
        ["keys", "add", "--project", str(pid), "--name", "files", "--plural", "--base", "%d files"]
    )
    assert rc == 0, p
    with Session(engine) as s:
        kid = keys_svc.get_key(s, pid, "files", "").id
    r = admin.post(
        "/projects/%d/keys/%d/base" % (pid, kid),  # noqa: UP031
        data={"text": "%d NEW files", "plural_category": "other", "csrf_token": csrf_for(admin)},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        base = keys_svc.base_values(s, s.get(StringKey, kid))
        assert base["other"] == "%d NEW files", base
        assert not base["one"] and not base["few"] and not base["many"], base
    print(
        "  [M] editing ONE plural form's base value on either surface leaves the "
        "other forms empty instead of re-fanning the singular string OK"
    )


def test_the_web_import_surfaces_its_notes_not_only_its_counts():
    """FIX-ROUND F4. The K1(c) floor's whole point is that a skipped write is NAMED --
    and the web import route flashed counts only, so `notes[]` reached the CLI,
    `--json` and /api and died on the ONE surface the download -> hand-off -> re-upload
    story actually happens on. "1 unchanged" is exactly what a silently skipped write
    looks like."""
    _auth(False)
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="de-DE")
    _add_key(pid, "settings", base="Settings", namespace="nav")
    with Session(engine) as s:
        keys_svc.set_value(
            s,
            keys_svc.get_key(s, pid, "settings", "nav"),
            "de-DE",
            "Einstellungen",
            origin=keys_svc.ORIGIN_HUMAN,
            actor="fixture",
        )
    client = TestClient(app)
    payload = _json.dumps({"settings": "Settings"}).encode()
    r = client.post(
        "/projects/%d/keys/import" % pid,  # noqa: UP031
        data={"locale": "de-DE", "fmt": "json", "namespace": "nav", "csrf_token": csrf_for(client)},
        files={"file": ("nav.json", payload, "application/json")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    html = client.get("/projects/%d/keys" % pid).text  # noqa: UP031
    assert "Last import:" in html, "the import's notes never reached the grid"
    assert "nav::settings" in html and "SOURCE text" in html, html
    again = client.get("/projects/%d/keys" % pid).text  # noqa: UP031
    assert "Last import:" not in again, "the notice must be one-shot"
    # ...and the stored translation is of course untouched.
    with Session(engine) as s:
        seg = keys_svc.values_for(s, keys_svc.get_key(s, pid, "settings", "nav"), "de-DE")[0]
        assert seg.target_text == "Einstellungen", seg.target_text
    print("  [M] a web import shows WHICH keys it skipped and why, not just the counts OK")


def test_the_grid_names_every_skipped_key_or_says_how_many_it_did_not():
    """FINAL ROUND, BLOCKER 2. F4 put the import's `notes[]` on the grid as a SENTENCE
    PER KEY, and the one-shot cookie is capped at 900 characters. A realistic upload --
    40 keys whose translations the K1(c) floor refused -- produced 20 notes, ~2,900
    characters, cut at 900: SEVEN of the forty keys were named, and the string ended
    mid-word ("... left ru-RU untransl"). Meanwhile `capabilities/keys.md` promised
    "the import names the keys it skipped ... on the grid after a browser upload".

    Names cost ~12 characters where a sentence costs ~150, so the same budget names
    all forty; when even names do not fit, the overflow is COUNTED (`+N more`) instead
    of the text being sliced in half."""
    _auth(False)
    cust = _mk_customer()
    pid = _mk_key_project(cust, targets="ru-RU")
    names = ["k%02d" % i for i in range(40)]  # noqa: UP031
    body = {n: f"Source text {n}" for n in names}
    client = TestClient(app)
    for locale in ("en-US", "ru-RU"):
        r = client.post(
            "/projects/%d/keys/import" % pid,  # noqa: UP031
            data={
                "locale": locale,
                "fmt": "json",
                "namespace": "nav",
                "csrf_token": csrf_for(client),
            },
            files={"file": ("nav.json", _json.dumps(body).encode(), "application/json")},
            follow_redirects=False,
        )
        assert r.status_code == 303, (locale, r.status_code)
    html = client.get("/projects/%d/keys" % pid).text  # noqa: UP031
    notice = html.split("Last import:")[1].split("</p>")[0]
    assert "40 value(s) were NOT written" in notice, notice
    missing = [n for n in names if ("nav::" + n) not in notice]
    assert not missing, ("keys NOT named on the grid", missing, notice)
    # ...and it is a whole sentence, not a stump: the cap never had to cut it.
    assert notice.rstrip().endswith("."), notice[-120:]
    assert len(notice) < keysweb._EXPORT_NOTICE_CAP, len(notice)
    # THE OVERFLOW HALF, at the service seam so it needs no 5,000-key upload: past the
    # budget the list is truncated on a NAME BOUNDARY and says how many it dropped.
    rep = {"skipped_keys": ["nav::key%03d" % i for i in range(500)], "notes": []}  # noqa: UP031
    line = key_files.import_notices(rep)[0]
    assert "500 value(s)" in line, line
    marker = line.split(", +")[-1]
    assert marker.startswith(("4", "3")) and "more" in marker, line
    shown = line.count("nav::key")
    dropped = int(marker.split(" ")[0])
    assert shown + dropped == 500, (shown, dropped, line)
    assert "nav::key%03d" % (shown - 1) in line, line  # cut on a name boundary  # noqa: UP031
    assert "nav::key%03d" % shown not in line, line  # noqa: UP031
    print(
        "  [M] a 40-key import names all forty on the grid, and an overflowing "
        "list is cut on a name boundary with the remainder counted OK"
    )


def test_a_query_parameter_cannot_reach_the_download_header():
    """FOUND WHILE FIXING F1 and worse than it: both download routes built
    `Content-Disposition` out of the `namespace` and `locale` QUERY PARAMETERS. An
    unknown namespace is not an error -- it just selects no keys -- so
    `?namespace=nav%0d%0aX-Injected:+yes` produced
    `attachment; filename="nav\\r\\nX-Injected: yes.ru-RU.json"` on the same
    unvalidated uvicorn writer F1 rides, and needed no write access whatsoever."""
    _auth(False)
    cust = _mk_customer()
    pid = _mk_key_project(cust)
    client = TestClient(app)
    for payload in ("nav\r\nX-Injected: yes", 'nav"; filename="evil.exe', "nav\\"):
        r = client.get(
            "/projects/%d/keys/export" % pid,  # noqa: UP031
            params={"locale": "de-DE", "namespace": payload},  # noqa: RUF100, UP031
        )
        assert r.status_code == 200, (payload, r.status_code)
        cd = r.headers["content-disposition"]
        assert all("\x20" <= ch <= "\x7e" for ch in cd), (payload, repr(cd))
        # ONE quoted filename, no escape out of it. The payload's TEXT survives as
        # inert characters, which is the point: data that reads like a header (or a
        # second filename) is not one.
        assert cd.count('"') == 2, (payload, cd)
        assert "\\" not in cd, (payload, cd)
        assert cd.startswith('attachment; filename="') and cd.endswith('.json"'), cd
    # ...and /api, which builds the same header from the same two parameters.
    _auth(True)
    try:
        tok = _key_for("keysurf-cd@x.com", scopes="api keys")
        r = TestClient(app).get(
            "/api/projects/%d/keys/export" % pid,  # noqa: UP031
            params={"locale": "de-DE", "namespace": "nav\r\nX-Injected: yes"},
            headers={"X-Loc-Token": tok},
        )
        assert r.status_code == 200, r.status_code
        cd = r.headers["content-disposition"]
        assert all("\x20" <= ch <= "\x7e" for ch in cd), repr(cd)
        assert cd.count('"') == 2, cd
    finally:
        _auth(False)
    print(
        "  [M] neither download route lets a query parameter put a control "
        "character or a quote into Content-Disposition OK"
    )


def test_the_download_reports_kept_damaged_values_too():
    """FIX-ROUND test debt (mutation k). Deleting `X-Export-Kept-Damaged[-Keys]` from
    the download survived the whole 211-suite sweep: transport kept-damaged reporting
    was asserted nowhere at the HTTP or `--remote` level, and those two headers are the
    ONLY channel a raw-bytes transport download has. The audit `detail` is asserted
    here too (fix-round F9) -- it used to be empty for exactly this shape."""
    _auth(False)
    cust = _mk_customer()
    pid = _mk_key_project(cust)
    _add_key(pid, "settings", base="Settings", namespace="nav")
    _damage(pid, "settings", "nav")
    client = TestClient(app)
    r = client.get("/projects/%d/keys/export?locale=de-DE" % pid)  # whole project  # noqa: UP031
    assert r.status_code == 200, r.status_code
    assert r.headers[key_files.HDR_KEPT] == "1", dict(r.headers)
    assert r.headers[key_files.HDR_KEPT_KEYS] == "nav::settings", dict(r.headers)
    assert key_files.HDR_SUBSTITUTED not in r.headers, dict(r.headers)
    html = client.get("/projects/%d/keys" % pid).text  # noqa: UP031
    assert "VERBATIM" in html, "the grid must name the kept-damaged values too"
    with Session(engine) as s:
        row = s.exec(
            select(AuditLog).where(AuditLog.action == "keys.export").order_by(AuditLog.id.desc())
        ).first()
        assert row is not None and "kept_damaged=1" in (row.detail or ""), row
    # ...and /api carries the same two headers, which is what --remote rebuilds from.
    _auth(True)
    try:
        tok = _key_for("keysurf-k-hdr@x.com", scopes="api keys")
        _remote._set_transport(_mock_transport(TestClient(app)))
        try:
            rc, p = _run_json(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    tok,
                    "keys",
                    "export",
                    "--project",
                    str(pid),
                    "--locale",
                    "de-DE",
                ]
            )
            assert rc == 0, p
            assert p["kept_damaged"] == 1, p
            assert p["kept_damaged_keys"] == ["nav::settings"], p
        finally:
            _remote._set_transport(None)
    finally:
        _auth(False)
    print(
        "  [M] a transport download reports its kept-damaged values in headers, in "
        "the audit row and back through --remote OK"
    )


def main():
    init_db()
    _switch(True)
    test_web_grid_crud_auth_on()
    test_web_grid_viewer_is_read_only()
    test_web_import_export_round_trip()
    test_create_form_type_selector()
    test_key_project_page_has_no_document_list()
    test_cli_local_verbs()
    test_cli_can_create_a_key_project()
    test_cli_cursor_file_round_trip()
    test_cli_remote_twins()
    test_api_write_gate_matches_the_web_one()
    test_api_put_value_surfaces_a_protection_refusal()
    test_api_put_plural_value_names_the_refused_forms()
    test_kill_switch_off_everywhere()
    test_cli_output_is_ascii()
    test_run_launcher_wraps_the_actor_scope()
    test_translate_all_locales_fans_out()
    test_cli_keys_translate_local_and_remote()
    test_cli_keys_translate_is_synchronous_in_process()
    test_213_keys_translate_conflicts_are_reported_on_every_surface()
    test_337_keys_translate_empty_locale_rest_conflicted_is_409()
    test_api_keys_translate_stays_async()
    test_deleted_keys_are_out_of_run_scope()
    test_internal_documents_are_invisible()
    test_no_run_can_target_an_internal_key_document()
    test_every_run_launcher_refuses_the_trash_document()
    test_the_editor_carve_out_stops_at_the_trash_document()
    test_a_docs_scoped_token_cannot_reach_key_internals()
    test_the_two_unguarded_segment_routes_refuse_a_deleted_key()
    test_the_cli_doc_verbs_have_the_key_document_gate()
    test_a_key_project_takes_no_documents()
    test_retention_never_ages_out_a_key_catalogue()
    test_a_cursor_file_from_another_project_bootstraps()
    test_remote_keys_add_refuses_a_duplicate_exactly_like_the_local_arm()
    test_restore_and_edit_are_headless_on_both_arms()
    test_a_patch_that_fails_applies_nothing()
    test_a_key_edit_walks_through_no_intermediate_identity()
    test_destructive_key_ops_are_audited()
    test_locale_changes_are_attributed_on_every_surface()
    test_api_list_excludes_archived_by_default()
    test_adding_a_locale_seeds_no_junk_into_a_key_document()
    test_the_autounverify_route()
    test_restore_fills_locales_added_while_tombstoned()
    test_a_key_project_shows_real_progress()
    test_project_export_on_a_key_project_names_the_key_surface()
    test_a_viewer_may_export_from_the_grid()
    test_one_flag_name_and_a_refused_limit()
    test_changes_echoes_the_effective_limit()
    test_the_switch_off_leaves_no_dead_links_and_names_no_type()
    test_an_export_tells_every_surface_what_the_tag_guard_did()
    test_the_withheld_report_reaches_the_cli_json_payload()
    test_a_singular_base_on_a_plural_key_says_it_filled_other_only()
    test_the_first_base_edit_does_not_re_fan_the_singular_string()
    test_the_web_import_surfaces_its_notes_not_only_its_counts()
    test_the_grid_names_every_skipped_key_or_says_how_many_it_did_not()
    test_a_query_parameter_cannot_reach_the_download_header()
    test_the_download_reports_kept_damaged_values_too()
    print("KEYS SURFACES OK")


if __name__ == "__main__":
    main()
