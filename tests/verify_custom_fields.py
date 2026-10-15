"""Custom project fields verification (SPEC-project-automation, MVP part C).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked). ZERO live calls.

Covers:

  schema            customfielddef + projectfieldvalue tables, one functional unique index
  service           every type (text, long_text, number, date, select, url, external_link),
                    normalization, required, unique keys per org (case-folded), key patterns,
                    option removal refused while used, archive/restore (values kept),
                    set/clear values, ALL-OR-NOTHING writes
  safe rendering    URL schemes allowlisted (javascript:, data:, http:, //host, credentials,
                    whitespace refused), HTML in values is escaped on the project page and the
                    list, a poisoned stored row never renders as a link
  filter + sort     = / ~ / >= / <= / != on each type, sort asc/desc with empty values last
  scoping           a customer-scoped field applies only to that customer's projects; a
                    customer-bound caller never sees another customer's fields or values;
                    a foreign org's field is a 404 (multi-org gate on, throwaway DB)
  /api              field CRUD (manager or admin; creator-or-admin, org-wide admin only), project create with custom_fields (required enforced,
                    a bad value creates NO project), PATCH values, list filter + sort,
                    viewer cannot write, foreign project 404
  web               settings page (create/update/archive), project fields form, projects list
                    columns + filters + sort, list column picker saved per user
  CLI               field create/list/update/archive, project create --field, set-field,
                    list --field/--columns/--sort-field, show
  audit             field.define / field.set / field.archive rows, long text hashed
  cascade           deleting a project removes its values; the phase-3 seam (set_values by an
                    integration actor) behaves like a person's write

Final line: "CUSTOM FIELDS OK".
"""

# ruff: noqa: E501  (long assertion messages in the fix-round tests)
import io
import json as _json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cf_")

from sqlalchemy import inspect
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    AuditLog,
    Customer,
    Organization,
    Project,
    ProjectFieldValue,
)
from app.services import (
    apikeys,
    content,
    security,
    settings,
)
from app.services import custom_fields as cf

init_db()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _cust(name):
    with Session(engine) as s:
        return content.customer_create(s, name).id


def _proj(name, cust_id, **kw):
    with Session(engine) as s:
        p = content.project_create(s, name, target_locales="fr-FR", customer_id=cust_id, **kw)
        return p.id


def _expect(fn, exc=cf.FieldError, what=""):
    try:
        fn()
    except exc:
        return
    _fail(f"expected {exc.__name__}: {what}")


# ============================================================================
def test_schema():
    insp = inspect(engine)
    names = set(insp.get_table_names())
    assert "customfielddef" in names and "projectfieldvalue" in names, names
    from sqlalchemy import text

    with engine.connect() as conn:
        idx = {
            r[0]
            for r in conn.execute(
                text(
                    "select name from sqlite_master "
                    "where type='index' and tbl_name='customfielddef'"
                )
            )
        }
    assert "ux_customfielddef_scope_key" in idx, idx
    print("  schema: OK")


def test_safe_url():
    ok = ["https://acme.atlassian.net/browse/LOC-1", "https://x.test/a?b=c#d"]
    bad = [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<script>1</script>",
        "http://insecure.test/x",
        "//evil.test/x",
        "/relative",
        "ftp://x.test/",
        "https://user:pw@x.test/",
        "https://x.test/a b",
        "https://x.test/\nfoo",
        "https:///nohost",
        "https://x.test\\@evil.test",
        "https://" + "a" * 2050,
        "",
        None,
        5,
    ]
    for u in ok:
        assert cf.safe_url(u) == u, u
    for u in bad:
        assert cf.safe_url(u) == "", u
    print("  URL allowlist (https only; script/data/http/relative/credentials refused): OK")


def test_types_and_values():
    c1 = _cust("Acme Types")
    pid = _proj("Types Proj", c1)
    with Session(engine) as s:
        defs = {}
        for key, label, typ, kw in [
            ("po_number", "PO number", "text", {"key_pattern": "PO-[0-9]{4}"}),
            ("notes", "Notes", "long_text", {}),
            ("budget", "Budget", "number", {}),
            ("kickoff", "Kickoff", "date", {}),
            ("tier", "Tier", "select", {"options": ["Gold", "Silver", "Bronze"]}),
            ("brief_url", "Brief", "url", {}),
            ("jira_ticket", "Jira ticket", "external_link", {"link_system": "jira"}),
        ]:
            defs[key] = cf.create_def(s, key, label, typ, **kw)
        project = s.get(Project, pid)
        cf.set_values(
            s,
            project,
            {
                "po_number": "PO-1234",
                "notes": "line 1\nline 2",
                "budget": "1,250.5",
                "kickoff": "2026-11-02",
                "tier": "silver",  # case-insensitive match, stored canonical
                "brief_url": "https://docs.test/brief",
                "jira_ticket": "https://acme.atlassian.net/browse/LOC-123",
            },
            actor="t@x.com",
        )
        vals = cf.project_values(s, project)
        assert vals["po_number"] == "PO-1234"
        assert vals["budget"] == 1250.5 and vals["kickoff"] == "2026-11-02"
        assert vals["tier"] == "Silver", vals["tier"]
        link = vals["jira_ticket"]
        assert link["key"] == "LOC-123" and link["system"] == "jira", link
        assert link["url"] == "https://acme.atlassian.net/browse/LOC-123"
        # a bare key is accepted for a jira field (no URL), pattern-checked
        cf.set_values(s, project, {"jira_ticket": "LOC-9"})
        assert cf.project_values(s, project)["jira_ticket"]["key"] == "LOC-9"
        assert cf.project_values(s, project)["jira_ticket"]["url"] == ""
        # object form with a label
        cf.set_values(
            s,
            project,
            {
                "jira_ticket": {
                    "url": "https://acme.atlassian.net/browse/LOC-7",
                    "label": "The ticket",
                }
            },
        )
        got = cf.project_values(s, project)["jira_ticket"]
        assert got["label"] == "The ticket" and got["key"] == "LOC-7", got
        # invalid values: nothing is written (all-or-nothing)
        before = cf.project_values(s, project)
        for bad in (
            {"po_number": "nope"},
            {"budget": "abc"},
            {"budget": "1e400"},
            {"kickoff": "31/12/2026"},
            {"tier": "Platinum"},
            {"brief_url": "javascript:alert(1)"},
            {"jira_ticket": "not a key"},
            {"jira_ticket": {"url": "javascript:alert(1)"}},
            {"jira_ticket": {"url": "https://x.test/", "bogus": "1"}},
            {"nope": "x"},
            {"notes": "x" * 10001},
            {"po_number": "x" * 501},
        ):
            _expect(lambda b=bad: cf.set_values(s, project, b), what=str(bad))
        _expect(
            lambda: cf.set_values(s, project, {"budget": 5, "tier": "Platinum"}),
            what="mixed good+bad",
        )
        assert cf.project_values(s, project) == before, "a failed write changed data"
        # clear
        cf.set_values(s, project, {"budget": None, "notes": ""})
        after = cf.project_values(s, project)
        assert "budget" not in after and "notes" not in after
    print("  every type: normalize, pattern, canonical select, links, all-or-nothing, clear: OK")


def test_definitions_rules():
    with Session(engine) as s:
        # keys: slug only, unique per org case-insensitively
        for bad in ("Bad Key", "1abc", "a-b", "", "x" * 41):
            _expect(lambda b=bad: cf.create_def(s, b, "L", "text"), what=bad)
        cf.create_def(s, "dupe_key", "Dupe", "text")
        _expect(lambda: cf.create_def(s, "DUPE_KEY", "Dupe 2", "text"), what="dup key")
        # select needs options; options only for select; pattern only for link/text
        _expect(lambda: cf.create_def(s, "sel0", "S", "select"), what="no options")
        _expect(
            lambda: cf.create_def(s, "num0", "N", "number", options=["a"]), what="opts on number"
        )
        _expect(
            lambda: cf.create_def(s, "num1", "N", "number", key_pattern="x"),
            what="pattern on number",
        )
        _expect(
            lambda: cf.create_def(s, "t0", "T", "text", link_system="jira"), what="system on text"
        )
        _expect(lambda: cf.create_def(s, "t1", "T", "bogus"), what="bad type")
        _expect(lambda: cf.create_def(s, "t2", "T", "text", key_pattern="(["), what="bad regex")
        _expect(
            lambda: cf.create_def(s, "t3", "T", "text", key_pattern="(a+)+$"),
            what="nested quantifier",
        )
        # update: key/type immutable; removing an option in use is refused
        d = cf.create_def(s, "color", "Color", "select", options=["red", "green"])
        _expect(lambda: cf.update_def(s, d, key="other"), what="key immutable")
        _expect(lambda: cf.update_def(s, d, type="text"), what="type immutable")
        pid = _proj("Opt Proj", _cust("Opt Cust"))
        cf.set_values(s, s.get(Project, pid), {"color": "red"})
        _expect(lambda: cf.update_def(s, d, options=["green"]), what="option in use")
        assert cf.update_def(s, d, options=["red", "green", "blue"]) == ["options"]
        assert cf.update_def(s, d, label="Colour", help_text="pick one") == ["label", "help_text"]
        # archive keeps values readable but refuses new edits
        assert cf.set_archived(s, d, True) is True and cf.set_archived(s, d, True) is False
        project = s.get(Project, pid)
        assert cf.project_values(s, project) == {}, "archived hidden by default"
        assert cf.project_values(s, project, include_archived=True)["color"] == "red"
        _expect(lambda: cf.set_values(s, project, {"color": "green"}), what="archived write")
        assert cf.set_archived(s, d, False) is True
        cf.set_values(s, project, {"color": "green"})
    print("  definitions: slug keys, unique, type rules, immutability, archive/restore: OK")


def test_required_on_create():
    with Session(engine) as s:
        cust = _cust("Req Cust")
        cf.create_def(s, "ref_code", "Ref code", "text", required=True)
        _expect(lambda: cf.validate_for_create(s, None, cust, {}), what="missing required")
        _expect(
            lambda: cf.validate_for_create(s, None, cust, {"ref_code": ""}), what="blank required"
        )
        plan = cf.validate_for_create(s, None, cust, {"ref_code": "A1"})
        assert len(plan) == 1
        # relax it so the rest of the suite is not forced to pass it
        d = cf.by_key(s, "ref_code")
        cf.update_def(s, d, required=False)
        # clearing a required field on an existing project is refused
        cf.update_def(s, d, required=True)
        pid = _proj("Req Proj", cust)
        project = s.get(Project, pid)
        cf.set_values(s, project, {"ref_code": "A1"})
        _expect(lambda: cf.set_values(s, project, {"ref_code": None}), what="clear required")
        cf.update_def(s, d, required=False)
        cf.set_values(s, project, {"ref_code": None})
    print("  required: enforced at create, clearing a required value refused: OK")


def test_filter_and_sort():
    cust = _cust("Filter Cust")
    names = ["F-A", "F-B", "F-C", "F-D"]
    pids = [_proj(n, cust) for n in names]
    with Session(engine) as s:
        cf.create_def(s, "f_num", "Num", "number")
        cf.create_def(s, "f_date", "Date", "date")
        cf.create_def(s, "f_txt", "Txt", "text")
        cf.create_def(s, "f_sel", "Sel", "select", options=["x", "y"])
        cf.create_def(s, "f_link", "Link", "external_link", link_system="jira")
        rows = [
            ("5", "2026-01-01", "alpha one", "x", "LOC-1"),
            ("10", "2026-06-01", "beta two", "y", "LOC-2"),
            ("20", "2026-12-01", "alpha three", "x", "LOC-30"),
            (None, None, None, None, None),
        ]
        for pid, (n, d, t, sl, lk) in zip(pids, rows, strict=True):
            cf.set_values(
                s,
                s.get(Project, pid),
                {"f_num": n, "f_date": d, "f_txt": t, "f_sel": sl, "f_link": lk},
            )

        def match(key, expr):
            d = cf.by_key(s, key)
            found = set(
                s.exec(
                    select(Project.id).where(Project.id.in_(cf.project_ids_matching(s, d, expr)))
                ).all()
            )
            return [names[pids.index(p)] for p in pids if p in found]

        assert match("f_num", "10") == ["F-B"]
        assert match("f_num", ">=10") == ["F-B", "F-C"]
        assert match("f_num", "<10") == ["F-A"]
        assert match("f_num", "!=10") == ["F-A", "F-C", "F-D"]
        assert match("f_date", ">=2026-06-01") == ["F-B", "F-C"]
        assert match("f_date", "2026-01-01") == ["F-A"]
        assert match("f_txt", "alpha") == ["F-A", "F-C"], "text defaults to contains"
        assert match("f_txt", "=beta two") == ["F-B"]
        assert match("f_sel", "x") == ["F-A", "F-C"]
        assert match("f_link", "loc-2") == ["F-B"], "exact key, case-insensitive"
        assert match("f_link", "~LOC-3") == ["F-C"], "contains also searches the link"
        _expect(lambda: match("f_txt", ">=5"), what="range on text")
        _expect(lambda: match("f_num", "abc"), what="non-numeric")
        _expect(lambda: match("f_date", "soon"), what="bad date")
        # a literal % is not a wildcard
        assert match("f_txt", "~%") == []
        # sort, empty values last in both directions
        for key, asc in (
            ("f_num", names[:3]),
            ("f_date", names[:3]),
            ("f_txt", ["F-A", "F-C", "F-B"]),
        ):
            d = cf.by_key(s, key)
            got = s.exec(
                cf.apply_sort(select(Project).where(Project.id.in_(pids)), d, descending=False)
            ).all()
            assert [names[pids.index(p.id)] for p in got] == [*asc, "F-D"], (
                key,
                [p.name for p in got],
            )
            got = s.exec(
                cf.apply_sort(select(Project).where(Project.id.in_(pids)), d, descending=True)
            ).all()
            assert [names[pids.index(p.id)] for p in got][-1] == "F-D", key
        # not filterable -> refused
        d = cf.create_def(s, "nofilter", "No filter", "text", filterable=False)
        _expect(lambda: cf.project_ids_matching(s, d, "x"), what="not filterable")
    print("  filter (= ~ >= <= !=, wildcards escaped) and sort (empty last) per type: OK")


def test_scoping_and_bound_customer():
    with Session(engine) as s:
        a, b = _cust("Scope A"), _cust("Scope B")
        gdef = cf.create_def(s, "sc_global", "Global", "text")
        adef = cf.create_def(s, "sc_a_only", "A only", "text", customer_id=a)
        pa, pb = _proj("Scope PA", a), _proj("Scope PB", b)
        proj_a, proj_b = s.get(Project, pa), s.get(Project, pb)
        assert {d.key for d in cf.defs_for_project(s, proj_a)} >= {"sc_global", "sc_a_only"}
        assert "sc_a_only" not in {d.key for d in cf.defs_for_project(s, proj_b)}
        _expect(lambda: cf.set_values(s, proj_b, {"sc_a_only": "x"}), what="foreign-customer field")
        cf.set_values(s, proj_a, {"sc_a_only": "secret-a", "sc_global": "g"})
        # a bound caller (customer B) never sees A's definition or values
        bound_b = {d.key for d in cf.list_defs(s, None, b)}
        assert "sc_a_only" not in bound_b and "sc_global" in bound_b
        assert cf.visible(s, adef.id, None, b) is None, "foreign customer field must 404"
        assert cf.visible(s, gdef.id, None, b) is not None
        vals = cf.payload_for_projects(s, [proj_a], None, b)
        assert "sc_a_only" not in vals[pa], vals
        # staff (unbound) sees all
        assert "sc_a_only" in cf.payload_for_projects(s, [proj_a], None, None)[pa]
    print("  scoping: customer field applies only to its customer; bound caller confined: OK")


def test_multi_org_isolation():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            o1 = Organization(name="CF Org 1")
            o2 = Organization(name="CF Org 2")
            s.add(o1)
            s.add(o2)
            s.commit()
            s.refresh(o1)
            s.refresh(o2)
            d1 = cf.create_def(s, "org_key", "Org one", "text", org_id=o1.id)
            # the same key in ANOTHER org is a separate field
            d2 = cf.create_def(s, "org_key", "Org two", "text", org_id=o2.id)
            assert d1.id != d2.id
            _expect(
                lambda: cf.create_def(s, "ORG_KEY", "dup", "text", org_id=o1.id), what="dup in org"
            )
            assert [d.id for d in cf.list_defs(s, o1.id) if d.key == "org_key"] == [d1.id]
            assert cf.visible(s, d2.id, o1.id) is None, "foreign org field must be invisible"
            # fail closed: a bare None org sees NOTHING under an active gate
            assert cf.list_defs(s, None) == []
            _expect(
                lambda: cf.create_def(s, "no_org", "x", "text", org_id=None),
                what="no org under gate",
            )
            # a project of org 1 gets org 1's field only
            cust = Customer(name="CF Org1 Cust", org_id=o1.id)
            s.add(cust)
            s.commit()
            s.refresh(cust)
            p = Project(name="CF Org1 P", customer_id=cust.id, org_id=o1.id)
            s.add(p)
            s.commit()
            s.refresh(p)
            assert [d.id for d in cf.defs_for_project(s, p) if d.key == "org_key"] == [d1.id]
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  multi-org: per-org keys, foreign field invisible, fail-closed on a bare None org: OK")


# --- /api ---------------------------------------------------------------------
_UN = 0


def _mk_user(role):
    global _UN
    _UN += 1
    return security.create_user(f"cf{role}{_UN}@x.com", role.title(), role, "custom-field-pw-123")


def _key(user):
    _row, raw = apikeys.create_key(user.id, "cf-test", scopes="api docs")
    return raw


def test_api():
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        admin = {"X-Loc-Token": _key(_mk_user("admin"))}
        manager = {"X-Loc-Token": _key(_mk_user("manager"))}
        viewer = {"X-Loc-Token": _key(_mk_user("viewer"))}
        # definitions: admin only for writes, anyone in-ring reads
        r = c.post("/api/custom-fields", headers=manager, json={"key": "api_x", "label": "X"})
        assert r.status_code == 403, r.text
        r = c.post(
            "/api/custom-fields",
            headers=admin,
            json={
                "key": "api_ticket",
                "label": "API ticket",
                "type": "external_link",
                "link_system": "jira",
                "show_in_list": True,
            },
        )
        assert r.status_code == 200, r.text
        fid = r.json()["id"]
        r = c.post("/api/custom-fields", headers=admin, json={"key": "API_TICKET", "label": "dup"})
        assert r.status_code == 400, r.text
        r = c.post(
            "/api/custom-fields",
            headers=admin,
            json={
                "key": "api_sev",
                "label": "Severity",
                "type": "select",
                "options": ["low", "high"],
                "required": True,
            },
        )
        assert r.status_code == 200, r.text
        sev_id = r.json()["id"]
        r = c.get("/api/custom-fields", headers=viewer)
        assert r.status_code == 200 and {"api_ticket", "api_sev"} <= {
            f["key"] for f in r.json()["fields"]
        }
        r = c.patch(f"/api/custom-fields/{fid}", headers=viewer, json={"label": "z"})
        assert r.status_code == 403, r.text
        r = c.patch(
            f"/api/custom-fields/{fid}", headers=admin, json={"label": "Tracker", "key": "zzz"}
        )
        assert r.status_code == 400, "key is immutable"
        r = c.patch(f"/api/custom-fields/{fid}", headers=admin, json={"label": "Tracker"})
        assert r.status_code == 200 and r.json()["label"] == "Tracker", r.text
        assert (
            c.patch("/api/custom-fields/99999", headers=admin, json={"label": "x"}).status_code
            == 404
        )

        with Session(engine) as s:
            cust = content.customer_create(s, "ApiCfCust").id
        # required field enforced; a bad value creates NO project
        n0 = _count_projects()
        r = c.post(
            "/api/projects",
            headers=admin,
            json={"name": "Api CF P0", "customer_id": cust, "targets": "fr-FR"},
        )
        assert r.status_code == 400 and "api_sev" in r.text, r.text
        r = c.post(
            "/api/projects",
            headers=admin,
            json={
                "name": "Api CF P0",
                "customer_id": cust,
                "targets": "fr-FR",
                "custom_fields": {"api_sev": "extreme"},
            },
        )
        assert r.status_code == 400, r.text
        r = c.post(
            "/api/projects",
            headers=admin,
            json={
                "name": "Api CF P0",
                "customer_id": cust,
                "targets": "fr-FR",
                "custom_fields": {"api_sev": "low", "api_ticket": "javascript:alert(1)"},
            },
        )
        assert r.status_code == 400, r.text
        assert _count_projects() == n0, "a rejected create left a project behind"
        r = c.post(
            "/api/projects",
            headers=admin,
            json={
                "name": "Api CF P1",
                "customer_id": cust,
                "targets": "fr-FR",
                "custom_fields": {"api_sev": "high", "api_ticket": "LOC-77"},
            },
        )
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        assert r.json()["custom_fields"]["api_sev"] == "high"
        assert r.json()["custom_fields"]["api_ticket"]["key"] == "LOC-77"
        # show + list carry the values
        r = c.get(f"/api/projects/{pid}", headers=manager)
        assert r.json()["custom_fields"]["api_sev"] == "high", r.text
        # PATCH values: viewer cannot, manager can; all-or-nothing
        r = c.patch(
            f"/api/projects/{pid}/fields", headers=viewer, json={"fields": {"api_sev": "low"}}
        )
        assert r.status_code in (403, 404), r.text  # ring (404) before role (403)
        r = c.patch(
            f"/api/projects/{pid}/fields",
            headers=manager,
            json={"fields": {"api_sev": "low", "api_ticket": "nonsense value"}},
        )
        assert r.status_code == 400, r.text
        assert (
            c.get(f"/api/projects/{pid}", headers=manager).json()["custom_fields"]["api_sev"]
            == "high"
        )
        r = c.patch(
            f"/api/projects/{pid}/fields", headers=manager, json={"fields": {"api_sev": "low"}}
        )
        assert r.status_code == 200 and r.json()["changed"] == ["api_sev"], r.text
        r = c.patch(f"/api/projects/{pid}/fields", headers=manager, json={"nope": 1})
        assert r.status_code == 400
        r = c.patch("/api/projects/99999/fields", headers=manager, json={"fields": {}})
        assert r.status_code == 404
        r = c.get(f"/api/projects/{pid}/fields", headers=manager)
        assert r.status_code == 200 and r.json()["custom_fields"]["api_sev"] == "low"
        # second project, then list filter + sort
        r = c.post(
            "/api/projects",
            headers=admin,
            json={
                "name": "Api CF P2",
                "customer_id": cust,
                "targets": "fr-FR",
                "custom_fields": {"api_sev": "high"},
            },
        )
        pid2 = r.json()["id"]
        r = c.get("/api/projects?field.api_sev=high", headers=manager)
        assert [p["id"] for p in r.json()["projects"]] == [pid2], r.text
        assert r.json()["projects"][0]["custom_fields"]["api_sev"] == "high"
        r = c.get("/api/projects?sort=field.api_sev&dir=desc", headers=manager)
        ids_sorted = [p["id"] for p in r.json()["projects"] if p["id"] in (pid, pid2)]
        assert ids_sorted == [pid, pid2], ids_sorted  # 'low' > 'high' descending
        assert c.get("/api/projects?field.nope=1", headers=manager).status_code == 400
        assert c.get("/api/projects?sort=name", headers=manager).status_code == 400
        # archive over the API: hidden from the default list, values kept
        r = c.patch(f"/api/custom-fields/{fid}", headers=admin, json={"archived": True})
        assert r.status_code == 200 and r.json()["archived"] is True
        assert fid not in {
            f["id"] for f in c.get("/api/custom-fields", headers=viewer).json()["fields"]
        }
        assert fid in {
            f["id"] for f in c.get("/api/custom-fields?archived=1", headers=viewer).json()["fields"]
        }
        c.patch(f"/api/custom-fields/{fid}", headers=admin, json={"archived": False})
        # relax required so later suites/tests can create projects freely
        c.patch(f"/api/custom-fields/{sev_id}", headers=admin, json={"required": False})
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  /api: admin-only schema, atomic create, PATCH values, filter/sort, archive: OK")


def _count_projects():
    with Session(engine) as s:
        return len(s.exec(select(Project.id)).all())


# --- web ----------------------------------------------------------------------
def test_web():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)  # auth off
    r = c.get("/settings/custom-fields")
    assert r.status_code == 200 and "Custom fields" in r.text
    r = c.post(
        "/settings/custom-fields",
        data={
            "key": "web_link",
            "label": "Web <b>link</b>",
            "type": "external_link",
            "link_system": "jira",
            "show_in_list": "on",
            "filterable": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    r = c.post(
        "/settings/custom-fields",
        data={
            "key": "web_note",
            "label": "Note",
            "type": "text",
            "show_in_list": "on",
            "filterable": "on",
        },
        follow_redirects=False,
    )
    assert "err=" not in r.headers["location"]
    r = c.post(
        "/settings/custom-fields",
        data={"key": "web_note", "label": "Dup", "type": "text"},
        follow_redirects=False,
    )
    assert "err=" in r.headers["location"], "duplicate key must flash an error"
    with Session(engine) as s:
        link_def = cf.by_key(s, "web_link")
        note_def = cf.by_key(s, "web_note")
    assert "Manage custom fields" in c.get("/projects?status=all").text, "admin link missing"
    page = c.get("/settings/custom-fields").text
    assert "Web &lt;b&gt;link&lt;/b&gt;" in page and "Web <b>link</b>" not in page, (
        "label not escaped"
    )

    cust = _cust("Web CF Cust")
    pid = _proj("Web CF Proj", cust)
    pid2 = _proj("Web CF Proj 2", cust)
    # project fields form
    r = c.post(
        f"/projects/{pid}/fields",
        data={
            "cf.web_link.url": "https://acme.atlassian.net/browse/LOC-5",
            "cf.web_link.key": "",
            "cf.web_link.label": "",
            "cf.web_note": "<script>alert(1)</script>",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    r = c.post(
        f"/projects/{pid2}/fields",
        data={"cf.web_note": "plain", "cf.web_link.url": "javascript:alert(1)"},
        follow_redirects=False,
    )
    assert "err=" in r.headers["location"], "unsafe URL must be refused"
    with Session(engine) as s:
        assert "web_note" not in cf.project_values(s, s.get(Project, pid2)), (
            "partial write on error"
        )
    page = c.get(f"/projects/{pid}").text
    assert "<script>alert(1)</script>" not in page, "value rendered unescaped on the project page"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert (
        'href="https://acme.atlassian.net/browse/LOC-5"' in page and "noopener noreferrer" in page
    )
    assert "LOC-5" in page
    # a POISONED stored row (older path / direct DB write) never becomes an active link
    with Session(engine) as s:
        row = s.exec(
            select(ProjectFieldValue).where(
                ProjectFieldValue.project_id == pid, ProjectFieldValue.field_id == link_def.id
            )
        ).first()
        row.value_json = _json.dumps(
            {"url": "javascript:alert(1)", "key": "LOC-5", "label": "", "system": "jira"}
        )
        s.add(row)
        s.commit()
    page = c.get(f"/projects/{pid}").text
    assert "javascript:alert(1)" not in page, "poisoned URL rendered"
    lst = c.get("/projects").text
    assert "javascript:alert(1)" not in lst and "<script>alert(1)</script>" not in lst
    # restore a good value for the list assertions
    with Session(engine) as s:
        cf.set_values(
            s, s.get(Project, pid), {"web_link": "https://acme.atlassian.net/browse/LOC-5"}
        )
        cf.set_values(s, s.get(Project, pid2), {"web_note": "zzz plain", "web_link": "LOC-1"})
    # list: default columns are the show_in_list fields; filter + sort via the query string
    lst = c.get("/projects?status=all").text
    assert "Web &lt;b&gt;link&lt;/b&gt;" in lst and ">Note<" in lst, "default columns missing"
    lst = c.get("/projects?status=all&f.web_note=zzz").text
    assert "Web CF Proj 2" in lst and "Web CF Proj</a>" not in lst.replace("Web CF Proj 2</a>", "")
    lst = c.get("/projects?status=all&sort=f.web_note&dir=asc").text
    assert lst.index("Web CF Proj 2") > 0
    # a bad filter does not 500
    assert c.get("/projects?f.web_note=%25%25&f.nope=1&sort=f.nope").status_code == 200
    # column picker is saved per owner and respected
    r = c.post("/projects/columns", data={"col": "web_note"}, follow_redirects=False)
    assert r.status_code == 303
    lst = c.get("/projects?status=all").text
    assert ">Note<" in lst and "Web &lt;b&gt;link&lt;/b&gt;</a>" not in lst.split("<tbody>")[0], (
        "picker ignored"
    )
    # update + archive
    r = c.post(
        f"/settings/custom-fields/{note_def.id}/update",
        data={"label": "Note 2", "filterable": "on"},
        follow_redirects=False,
    )
    assert "err=" not in r.headers["location"]
    r = c.post(f"/settings/custom-fields/{note_def.id}/archive", follow_redirects=False)
    assert r.status_code == 303
    page = c.get(f"/projects/{pid2}").text
    assert "Note 2" in page, "archived field with a value stays visible on the project"
    assert c.post("/settings/custom-fields/99999/archive").status_code == 404
    c.post(f"/settings/custom-fields/{note_def.id}/restore")
    # the new-project dialog carries the fields (hidden JSON + per-field inputs)
    home = c.get("/projects").text
    assert "data-np-cf-json" in home and 'data-cf-key="web_note"' in home
    # create a project through the dialog's POST with the serialized JSON
    r = c.post(
        "/projects",
        data={
            "name": "Web CF Created",
            "customer_id": str(cust),
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "mock",
            "custom_fields_json": _json.dumps({"web_note": "from dialog"}),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    with Session(engine) as s:
        made = s.exec(select(Project).where(Project.name == "Web CF Created")).first()
        assert made is not None and cf.project_values(s, made)["web_note"] == "from dialog"
    r = c.post(
        "/projects",
        data={
            "name": "Web CF Bad",
            "customer_id": str(cust),
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "mock",
            "custom_fields_json": _json.dumps({"web_link": {"url": "javascript:alert(1)"}}),
        },
        follow_redirects=False,
    )
    assert "err=" in r.headers["location"]
    with Session(engine) as s:
        assert s.exec(select(Project).where(Project.name == "Web CF Bad")).first() is None
    print("  web: schema editor, project form, escaping, poisoned row inert, list columns: OK")


# --- CLI ----------------------------------------------------------------------
def test_cli():
    rc, out = _run_cli(
        [
            "field",
            "create",
            "cli_ticket",
            "CLI ticket",
            "--type",
            "external_link",
            "--system",
            "jira",
            "--list-column",
        ]
    )
    assert rc == 0, out
    rc, out = _run_cli(
        ["field", "create", "cli_prio", "Priority", "--type", "select", "--options", "p1,p2,p3"]
    )
    assert rc == 0, out
    rc, out = _run_cli(["field", "create", "cli_prio", "Dup"])
    assert rc == 2, ("duplicate key must be a usage error", rc, out)
    rc, out = _run_cli(["--json", "field", "list"])
    assert rc == 0 and {"cli_ticket", "cli_prio"} <= {
        f["key"] for f in _json.loads(out)["fields"]
    }, out
    with Session(engine) as s:
        fid = cf.by_key(s, "cli_prio").id
    rc, out = _run_cli(["field", "update", str(fid), "--label", "Prio", "--required", "off"])
    assert rc == 0, out
    rc, out = _run_cli(["field", "update", str(fid)])
    assert rc == 2, "update with nothing to change"
    cust = _cust("CLI CF Cust")
    rc, out = _run_cli(
        [
            "--json",
            "project",
            "create",
            "CLI CF P1",
            "--customer-id",
            str(cust),
            "--targets",
            "fr-FR",
            "--field",
            "cli_ticket=LOC-42",
            "--field",
            "cli_prio=p2",
        ]
    )
    assert rc == 0, out
    pid = _json.loads(out)["id"]
    assert _json.loads(out)["custom_fields"]["cli_prio"] == "p2"
    n0 = _count_projects()
    rc, out = _run_cli(
        [
            "project",
            "create",
            "CLI CF P2",
            "--customer-id",
            str(cust),
            "--targets",
            "fr-FR",
            "--field",
            "cli_prio=p9",
        ]
    )
    assert rc == 2 and _count_projects() == n0, ("bad --field must create nothing", rc, out)
    rc, out = _run_cli(
        [
            "project",
            "create",
            "CLI CF P2",
            "--customer-id",
            str(cust),
            "--targets",
            "fr-FR",
            "--field",
            "nokey",
        ]
    )
    assert rc == 2
    rc, out = _run_cli(["project", "set-field", str(pid), "cli_prio", "p3"])
    assert rc == 0, out
    rc, out = _run_cli(["project", "set-field", str(pid), "cli_prio", "nope"])
    assert rc == 2, out
    rc, out = _run_cli(["--json", "project", "show", str(pid)])
    shown = _json.loads(out)["custom_fields"]
    assert shown["cli_prio"] == "p3" and shown["cli_ticket"]["key"] == "LOC-42", shown
    rc, out = _run_cli(["project", "show", str(pid)])
    assert "field cli_prio: p3" in out, out
    pid_b = _proj("CLI CF P3", cust)
    rc, out = _run_cli(["project", "set-field", str(pid_b), "cli_prio", "p1"])
    assert rc == 0
    rc, out = _run_cli(["--json", "project", "list", "--field", "cli_prio=p1"])
    got = [p["id"] for p in _json.loads(out)["projects"]]
    assert got == [pid_b], got
    rc, out = _run_cli(
        ["project", "list", "--columns", "cli_prio,cli_ticket", "--sort-field", "cli_prio:desc"]
    )
    assert rc == 0 and "cli_prio" in out.splitlines()[0] and "LOC-42" in out, out
    rc, out = _run_cli(["project", "list", "--field", "nope=1"])
    assert rc == 2
    rc, out = _run_cli(["project", "set-field", str(pid), "cli_prio", ""])
    assert rc == 0
    with Session(engine) as s:
        assert "cli_prio" not in cf.project_values(s, s.get(Project, pid))
    rc, out = _run_cli(["field", "archive", str(fid)])
    assert rc == 0
    rc, out = _run_cli(["--json", "field", "list"])
    assert "cli_prio" not in {f["key"] for f in _json.loads(out)["fields"]}
    rc, out = _run_cli(["--json", "field", "list", "--archived"])
    assert "cli_prio" in {f["key"] for f in _json.loads(out)["fields"]}
    rc, out = _run_cli(["field", "restore", str(fid)])
    assert rc == 0
    print("  CLI: field verbs, project create --field, set-field, show, list --field/--columns: OK")


# --- audit, cascade, phase-3 seam -----------------------------------------------
def test_audit_cascade_seam():
    cust = _cust("Audit CF Cust")
    pid = _proj("Audit CF P", cust)
    with Session(engine) as s:
        cf.create_def(s, "au_long", "Long", "long_text")
        cf.create_def(s, "au_ticket", "Ticket", "external_link", link_system="jira")
        project = s.get(Project, pid)
        # the phase-3 seam: an integration actor writes the created key like a person
        changes = cf.set_values(s, project, {"au_ticket": "LOC-500"}, actor="integration:jira")
        assert changes and changes[0]["new"] == "LOC-500", changes
        row = s.exec(select(ProjectFieldValue).where(ProjectFieldValue.project_id == pid)).first()
        assert row.updated_by == "integration:jira"
        # idempotent: writing the same value again changes nothing
        assert cf.set_values(s, project, {"au_ticket": "LOC-500"}) == []
        ch = cf.set_values(s, project, {"au_long": "private text about the client"})
        assert "private text" not in cf.audit_detail(ch) and "sha256:" in cf.audit_detail(ch), ch
    # a value change through the web writes a field.set audit row
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    c.post(f"/projects/{pid}/fields", data={"cf.au_ticket.key": "LOC-501"})
    c.post("/settings/custom-fields", data={"key": "au_made", "label": "Made", "type": "text"})
    with Session(engine) as s:
        actions = {a.action for a in s.exec(select(AuditLog)).all()}
        assert {"field.define", "field.set"} <= actions, actions
        det = [
            a.detail for a in s.exec(select(AuditLog).where(AuditLog.action == "field.set")).all()
        ]
        assert any("LOC-501" in (d or "") for d in det), det
    # deleting the project removes its values (the definitions stay)
    with Session(engine) as s:
        content.delete_project(s, pid, actor="t@x.com")
    with Session(engine) as s:
        assert (
            s.exec(select(ProjectFieldValue).where(ProjectFieldValue.project_id == pid)).all() == []
        )
        assert cf.by_key(s, "au_ticket") is not None
    print("  audit (long text hashed), phase-3 seam (integration actor), delete cascade: OK")


# ============================================================================
# FIX ROUND 1 (review SONNET-REVIEW.md): each test below failed before its fix.
# ============================================================================
def test_r1_h1_pattern_redos():
    import time

    # the reviewer's three bypasses, plus the classic shapes, are refused at definition time
    for bad in (
        "(a|aa)+",
        "((a+))+",
        "(?:a|a)*",
        "(a+)+$",
        "(a*)*",
        "(.*a){5}",
        r"(a)\1",
        "(?=a)a+",
        "a*a*a*a*b",
        "a{1,500}",
        "x" * 201,
    ):
        with Session(engine) as s:
            _expect(lambda b=bad: cf.create_def(s, "redos_t", "T", "text", key_pattern=b), what=bad)
    # patterns people really need still work, and a hostile value cannot stall a worker
    with Session(engine) as s:
        cf.create_def(s, "po_safe", "PO", "text", key_pattern="PO-[0-9]{4,6}")
        cf.create_def(s, "jira_safe", "J", "external_link", link_system="jira")
        pid = _proj("ReDoS Proj", _cust("ReDoS Cust"))
        p = s.get(Project, pid)
        t0 = time.time()
        _expect(lambda: cf.set_values(s, p, {"po_safe": "PO-" + "1" * 400}), what="long value")
        _expect(lambda: cf.set_values(s, p, {"jira_safe": "A" * 5000}), what="long key")
        long_url = "https://x.test/" + "a" * 1900
        cf.set_values(s, p, {"jira_safe": long_url})  # accepted as a plain link, searched capped
        assert time.time() - t0 < 2.0, "pattern matching on hostile input was slow"
        cf.set_values(s, p, {"po_safe": "PO-12345", "jira_safe": "LOC-7"})
    # the unit under test directly: the three reviewer patterns report a reason
    for bad in ("(a|aa)+", "((a+))+", "(?:a|a)*"):
        assert cf._pattern_problem(bad), bad
    assert cf._pattern_problem(r"[A-Z][A-Z0-9_]+-\d+") == ""
    print("  r1 H1: key_pattern ReDoS shapes refused, input capped, real patterns still work: OK")


def test_gate_bounded_repeat_redos():
    """Review gate 10-13: BOUNDED repeats are variable-width too. `a{0,100}` six times (13 s
    on 120 characters) and the textbook `a?`*n `a`*n were admitted by the round-1 checker,
    which only counted unbounded repeats. Every variable-length repeat now counts."""
    import time

    for bad in ("a{0,100}" * 6 + "b", "[a-z]{0,100}" * 8 + "X", "a?" * 30 + "a" * 30, "a{0,9}" * 4):
        assert cf._pattern_problem(bad), bad
        with Session(engine) as s:
            _expect(lambda b=bad: cf.create_def(s, "redos_g", "T", "text", key_pattern=b), what=bad)
    # the worst still-admitted shapes stay fast on the longest input ever matched
    for ok in ("a{0,100}a{0,100}a{0,100}b", ".*.*.*!", r"[A-Z]{2,10}-\d{1,8}", r"\d{4}-\d{4}"):
        assert cf._pattern_problem(ok) == "", ok
        t0 = time.time()
        cf._pattern_match(ok, "a" * cf.MAX_MATCH_INPUT)
        assert time.time() - t0 < 1.0, ok
    print("  gate: bounded variable repeats count toward the ReDoS cap: OK")


def test_r1_h2_customer_delete():
    from app.services import customers as customer_service

    with Session(engine) as s:
        cid = content.customer_create(s, "Delete Cust CF").id
        cf.create_def(s, "del_cust_f", "Del", "text", customer_id=cid)
        refs = customer_service.reference_count(s, cid)
        assert refs.get("custom_fields") == 1 and refs["total"] >= 1, refs
        try:
            customer_service.delete_customer(s, cid, actor="t@x.com")
            _fail("a customer with a custom field must not be deletable")
        except customer_service.CustomerDeleteBlocked:
            pass
        # archive the field is not enough (the row still references the customer); delete path is
        # reassign/remove first, so the block stays
        d = cf.by_key(s, "del_cust_f")
        cf.set_archived(s, d, True)
        assert customer_service.reference_count(s, cid)["custom_fields"] == 1
    print("  r1 H2: customer delete counts and blocks on custom fields (no FK 500 / orphan): OK")


def test_r1_m1_key_per_scope():
    with Session(engine) as s:
        a, b = _cust("Scope Key A"), _cust("Scope Key B")
        cf.create_def(s, "shared_po", "PO A", "text", customer_id=a)
        cf.create_def(s, "shared_po", "PO B", "text", customer_id=b)  # other customer: allowed
        _expect(
            lambda: cf.create_def(s, "SHARED_PO", "dup", "text", customer_id=a), what="same scope"
        )
        _expect(
            lambda: cf.create_def(s, "shared_po", "global clash", "text"), what="global vs customer"
        )
        cf.create_def(s, "global_only_k", "G", "text")
        _expect(
            lambda: cf.create_def(s, "global_only_k", "clash", "text", customer_id=a),
            what="customer vs global",
        )
        pa, pb = _proj("SK PA", a), _proj("SK PB", b)
        cf.set_values(s, s.get(Project, pa), {"shared_po": "A-1"})
        cf.set_values(s, s.get(Project, pb), {"shared_po": "B-1"})
        defs = cf.group_by_key(cf.list_defs(s, None))["shared_po"]
        assert len(defs) == 2
        hit = set(
            s.exec(
                select(Project.id).where(Project.id.in_(cf.project_ids_matching(s, defs, "A-1")))
            ).all()
        )
        assert hit == {pa}, hit
        got = [
            p.id
            for p in s.exec(
                cf.apply_sort(select(Project).where(Project.id.in_([pa, pb])), defs)
            ).all()
        ]
        assert got == [pa, pb], got
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    html = c.get("/projects?status=all&f.shared_po=B-1").text
    assert "SK PB" in html and "SK PA" not in html
    print(
        "  r1 M1: key unique per (org, customer) scope, global-vs-customer clash refused, filter/sort span both: OK"
    )


def test_r1_m2_kill_switch():
    cust = _cust("Switch Cust")
    pid = _proj("Switch Proj", cust)
    with Session(engine) as s:
        d = cf.create_def(s, "sw_f", "Sw", "text", show_in_list=True)
        cf.set_values(s, s.get(Project, pid), {"sw_f": "zz-sw-9"})
    settings.set_many({"custom_fields_enabled": "0"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            p = s.get(Project, pid)
            assert cf.list_defs(s, None) == [] and cf.defs_for_project(s, p) == []
            assert cf.project_values(s, p) == {}
            for fn in (
                lambda: cf.set_values(s, p, {"sw_f": "x"}),
                lambda: cf.create_def(s, "sw_new", "New", "text"),
                lambda: cf.update_def(s, d, label="L"),
                lambda: cf.set_archived(s, d, True),
            ):
                _expect(fn, what="write while off")
        from fastapi.testclient import TestClient

        from app.main import app

        c = TestClient(app)
        assert c.get("/projects?status=all").status_code == 200
        page = c.get(f"/projects/{pid}").text
        assert "Edit project fields" not in page and "zz-sw-9" not in page
        r = c.get("/settings/custom-fields")
        assert r.status_code == 200 and "switched off" in r.text
    finally:
        settings.set_many({"custom_fields_enabled": "1"})
        settings.invalidate_cache()
    with Session(engine) as s:
        assert cf.project_values(s, s.get(Project, pid)) == {"sw_f": "zz-sw-9"}, (
            "values must survive"
        )
    from app import features, modules

    assert any(m.slug == "custom_fields" and m.semantics == "writes_off" for m in modules.MODULES)
    assert features.is_enabled("custom_fields")
    print(
        "  r1 M2: writes_off module custom_fields_enabled hides fields/forms, refuses writes, keeps values: OK"
    )


def test_r1_m3_api_bad_bodies():
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        admin = {"X-Loc-Token": _key(_mk_user("admin"))}
        r = c.post("/api/custom-fields", headers=admin, json={"key": "m3_f", "label": "M3"})
        fid = r.json()["id"]
        for body in (
            {"session": 1},
            {"defn": 1},
            {"changes": 1},
            {"required": "false"},
            {"show_in_list": "no"},
            {"archived": "false"},
            {"list_order": "x"},
            {"zzz": 1},
        ):
            r = c.patch(f"/api/custom-fields/{fid}", headers=admin, json=body)
            assert r.status_code == 400, (body, r.status_code, r.text)
        r = c.post(
            "/api/custom-fields",
            headers=admin,
            json={"key": "m3_g", "label": "G", "required": "false"},
        )
        assert r.status_code == 400, r.text
        # L4: scope first, validate second on project create
        r = c.post(
            "/api/projects",
            headers=admin,
            json={"name": "L4", "customer_id": 999999, "custom_fields": {"nope": "x"}},
        )
        assert r.status_code == 404, (r.status_code, r.text)
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print(
        "  r1 M3/L4: unexpected or non-boolean PATCH/POST body keys are a clean 400, ring before validation: OK"
    )


def test_r1_m4_gdpr_sweep():
    from app.services import gdpr_common

    cols = {(m.__name__, getattr(c, "key", None)) for _n, m, c in gdpr_common._ERASE_SWEEP}
    assert ("CustomFieldDef", "created_by") in cols and (
        "ProjectFieldValue",
        "updated_by",
    ) in cols, cols
    print("  r1 M4: created_by / updated_by are in the GDPR erase sweep: OK")


def test_r1_m5_value_key_and_indexes():
    from sqlalchemy import text

    cust = _cust("ValKey Cust")
    pid = _proj("ValKey P", cust)
    with Session(engine) as s:
        cf.create_def(s, "vk_sel", "VK", "select", options=["Gold", "Silver"])
        cf.set_values(s, s.get(Project, pid), {"vk_sel": "gold"})
        row = s.exec(select(ProjectFieldValue).where(ProjectFieldValue.project_id == pid)).first()
        assert row.value_text == "Gold" and row.value_key == "gold", (row.value_text, row.value_key)
    with engine.connect() as conn:
        idx = {
            r[0]
            for r in conn.execute(
                text(
                    "select name from sqlite_master where type='index' and tbl_name='projectfieldvalue'"
                )
            )
        }
    assert {
        "ix_projectfieldvalue_field_key",
        "ix_projectfieldvalue_field_num",
        "ix_projectfieldvalue_field_date",
    } <= idx, idx
    assert cf.columns_for(Session(engine), "local", []) == [], "no definitions -> no extra queries"
    print(
        "  r1 M5: lowercased value_key + number/date/key indexes, empty-definition short circuit: OK"
    )


def test_r1_lows():
    from app.models import CustomFieldDef
    from app.services import custom_fields as m

    # L2 numbers
    cust = _cust("Lows Cust")
    pid = _proj("Lows P", cust)
    with Session(engine) as s:
        cf.create_def(s, "lw_num", "Num", "number")
        p = s.get(Project, pid)
        _expect(lambda: cf.set_values(s, p, {"lw_num": "1,5"}), what="1,5 must not become 15")
        cf.set_values(s, p, {"lw_num": "1,250.5"})
        assert cf.project_values(s, p)["lw_num"] == 1250.5
        d = cf.by_key(s, "lw_num")
        _expect(lambda: cf.project_ids_matching(s, d, "nan"), what="nan filter")
        _expect(lambda: cf.project_ids_matching(s, d, "1,5"), what="1,5 filter")
        # L1 concurrent first write: the other writer's row appears between plan and commit
        cf.create_def(s, "lw_race", "Race", "text")
        plan = m._resolve(s, p, {"lw_race": "mine"})
        real_row = m._row
        calls = {"n": 0}

        def racing_row(session, project_id, field_id):
            calls["n"] += 1
            if calls["n"] == 1:
                with Session(engine) as other:
                    other.add(
                        ProjectFieldValue(
                            project_id=project_id,
                            field_id=field_id,
                            value_text="theirs",
                            value_key="theirs",
                        )
                    )
                    other.commit()
                return None
            return real_row(session, project_id, field_id)

        m._row = racing_row
        try:
            changes = m.apply_plan(s, p, plan, actor="t@x.com")
        finally:
            m._row = real_row
        assert cf.project_values(s, p)["lw_race"] == "mine" and changes, changes
        # L3 legacy NULL-org definitions are not settable under an active gate
        settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
        settings.invalidate_cache()
        try:
            o = Organization(name="L3 Org")
            s.add(o)
            s.commit()
            s.refresh(o)
            legacy = CustomFieldDef(org_id=None, key="legacy_null", label="Legacy", type="text")
            s.add(legacy)
            s.commit()
            proj = Project(name="L3 P", org_id=o.id)
            s.add(proj)
            s.commit()
            s.refresh(proj)
            assert "legacy_null" not in {d.key for d in cf.defs_for_project(s, proj)}
        finally:
            settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
            settings.invalidate_cache()
        # L6 audit detail carries values; long detail is marked as clipped
        before = cf.to_dict(d)
        cf.update_def(s, d, help_text="new help")
        detail = cf.change_detail(before, cf.to_dict(d), ["help_text"])
        assert "'new help'" in detail, detail
        assert cf.audit_detail([{"key": "k", "old": "x" * 500, "new": "y" * 500}]).endswith("...")
    # L5 accessibility bits
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    c.post(
        "/settings/custom-fields",
        data={
            "key": "l5_req",
            "label": "L5 Req",
            "type": "text",
            "required": "on",
            "show_in_list": "on",
            "filterable": "on",
        },
    )
    c.post("/projects/columns", data={"col": "l5_req"})  # the local owner saved a picker earlier
    html = c.get("/projects?status=all&sort=f.l5_req&dir=asc").text
    assert 'aria-sort="ascending"' in html, "sorted column header needs aria-sort"
    assert 'name="archived" value="only"' in c.get("/projects?status=all&archived=only").text, (
        "field filter form dropped the archived filter"
    )
    assert 'aria-required="true"' in html, "required field in the dialog needs aria-required"
    with Session(engine) as s:
        d5 = cf.by_key(s, "l5_req")
        cf.update_def(s, d5, required=False)
    print(
        "  r1 L1-L6: concurrent write retry, comma numbers, NULL-org, audit values, aria-sort/required/filters: OK"
    )


def main():
    print("custom fields verification")
    test_schema()
    test_safe_url()
    test_types_and_values()
    test_definitions_rules()
    test_required_on_create()
    test_filter_and_sort()
    test_scoping_and_bound_customer()
    test_multi_org_isolation()
    test_api()
    test_web()
    test_cli()
    test_audit_cascade_seam()
    test_r1_h1_pattern_redos()
    test_gate_bounded_repeat_redos()
    test_r1_h2_customer_delete()
    test_r1_m1_key_per_scope()
    test_r1_m2_kill_switch()
    test_r1_m3_api_bad_bodies()
    test_r1_m4_gdpr_sweep()
    test_r1_m5_value_key_and_indexes()
    test_r1_lows()
    print("CUSTOM FIELDS OK")


if __name__ == "__main__":
    main()
