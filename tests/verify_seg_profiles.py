"""Segmentation profiles verification (P6, item 8), offline + hermetic.

Covers:
  1. Profile CRUD: create (built-in + custom blob), clone-before-edit, metadata
     update, the single-system-default invariant, delete + in-use delete guard.
  2. Legacy custom-.srx migration -> "Custom (migrated)" profile (idempotent).
  3. The resolution chain: project override -> customer default -> system
     default -> ACTIVE ruleset (incl. the legacy-custom-upload compat rung).
  4. Customer-default isolation: a profile private to another customer is
     rejected (P4 bar); project assign validates the same way.
  5. Ingest segments under the RESOLVED profile (a semicolon-break SRX splits
     differently from the default) and stamps seg_engine with THAT ruleset's
     hash; srx_enabled=0 ignores the chain entirely (legacy stamp, sacred gate).
  6. Web: /settings profile create/clone/default/delete routes + project
     segmentation POST (write-gated) + delete guard.
  7. CLI: segprofile list/show/create/clone/set-default/assign/delete
     round-trip + project create --seg-profile (blob never echoed).
  8. Day-1a review #1: multi-org gate ON, an org-A admin gets not-found on org-B's
     profile via clone/default/delete (never enumerable, never mutable).
  9. Day-1a review #4: multi-org gate ON, the system-default rung is PER-ORG --
     org A's project resolves only org A's default; an org-A admin cannot
     clear/seize org B's default slot.
  10. Day-1a review #5: `project create --seg-profile` with a bad/foreign id
      fails the WHOLE create on both CLI and web -- no orphaned project row.
  11. #451: reingest's locale set is the project's target locales merged with the
      locales on existing segments (a zero-segment repair is not placeholder-only).

Prints ASCII only (cp1252 console safe). Final line: SEG PROFILES OK
"""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Organization,
    Project,
    Role,
    Segment,
    SegmentationProfile,
    TargetLocale,
    TransMode,
    User,
)
from app.services import (
    customers as customer_service,
)
from app.services import (
    security,
    settings,
    srx,
)
from app.services import (
    segmentation_profiles as segprof,
)
from tests._csrf import csrf_for

init_db()
client = TestClient(app, follow_redirects=False)

# A ruleset that breaks on semicolons — clearly different from the default rules,
# so "which profile segmented this?" is observable from the segment count.
SEMI_SRX = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<srx xmlns="http://www.lisa.org/srx20" version="2.0">'
    '<header segmentsubflows="no" cascade="yes">'
    '<formathandle type="start" include="no"/>'
    '<formathandle type="end" include="yes"/>'
    '<formathandle type="isolated" include="no"/></header>'
    "<body><languagerules>"
    '<languagerule languagerulename="Semi">'
    '<rule break="yes"><beforebreak>;</beforebreak><afterbreak>\\s</afterbreak></rule>'
    "</languagerule></languagerules>"
    '<maprules><languagemap languagepattern=".*" languagerulename="Semi"/></maprules>'
    "</body></srx>"
)

TEXT = "Alpha part; beta part; gamma part."


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def run_json(argv):
    code, out, err = run(["--json"] + argv)  # noqa: RUF005
    payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    return code, payload, err


def _mkproject(name, customer_id=None):
    with Session(engine) as s:
        p = Project(name=name, source_locale="en-US", customer_id=customer_id)
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        return p.id


def _ingest(project_id, text=TEXT):
    from app.services.ingest import ingest_document

    with Session(engine) as s:
        proj = s.get(Project, project_id)
        doc = ingest_document(s, proj, "sample.txt", text.encode("utf-8"), TransMode.SEGMENTED)
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == "fr-FR")
        ).all()
        skel = s.get(Document, doc.id).skeleton or {}
        return doc.id, len(segs), skel.get("seg_engine", "")


# --- 1. CRUD + invariants -----------------------------------------------------
def test_crud():
    prof = segprof.create_profile("Built-in copy", description="uses default rules")
    assert prof.id and prof.srx_blob is None
    custom = segprof.create_profile("Semi rules", srx_blob=SEMI_SRX)
    assert custom.srx_blob
    # unique name
    try:
        segprof.create_profile("Semi rules")
        raise AssertionError("duplicate name accepted")
    except segprof.SegProfileError:
        pass
    # invalid blob rejected
    try:
        segprof.create_profile("Bad", srx_blob="<srx>not valid")
        raise AssertionError("invalid blob accepted")
    except segprof.SegProfileError:
        pass
    # clone copies the blob, never the default flag
    a = segprof.update_profile(custom.id, is_system_default=True)
    assert a.is_system_default
    clone = segprof.clone_profile(custom.id, "Semi rules v2")
    assert clone.srx_blob == SEMI_SRX and not clone.is_system_default
    # single-system-default invariant
    b = segprof.update_profile(clone.id, is_system_default=True)
    assert b.is_system_default
    assert not segprof.get_profile(custom.id).is_system_default
    segprof.update_profile(clone.id, is_system_default=False)
    # delete
    assert segprof.delete_profile(prof.id)
    assert segprof.get_profile(prof.id) is None
    print("  crud + invariants ok")


# --- 2. legacy migration --------------------------------------------------------
def test_legacy_migration():
    assert segprof.migrate_legacy_custom() is None  # nothing configured -> no-op
    path = srx.custom_srx_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(SEMI_SRX.encode("utf-8"))
    settings.set_many({"srx_file": "segmentation.srx"})
    m1 = segprof.migrate_legacy_custom()
    assert m1 is not None and m1.name == "Custom (migrated)" and m1.srx_blob
    m2 = segprof.migrate_legacy_custom()
    assert m2.id == m1.id  # idempotent
    segprof.delete_profile(m1.id)
    settings.set_many({"srx_file": ""})
    print("  legacy custom-srx migration ok")


# --- 3+4. resolution chain + isolation ----------------------------------------
def test_chain_and_isolation():
    with Session(engine) as s:
        ca = Customer(name="Cust A")
        cb = Customer(name="Cust B")
        s.add(ca)
        s.add(cb)
        s.commit()
        s.refresh(ca)
        s.refresh(cb)
    glob = segprof.create_profile("Chain global", srx_blob=SEMI_SRX)
    mine = segprof.create_profile("A private", srx_blob=SEMI_SRX, customer_id=ca.id)
    other = segprof.create_profile("B private", srx_blob=SEMI_SRX, customer_id=cb.id)
    pid = _mkproject("Chain proj", customer_id=ca.id)
    with Session(engine) as s:
        proj = s.get(Project, pid)
        # rung 4: nothing set anywhere -> active ruleset (== default here)
        assert segprof.resolve_profile(s, proj) is None
        assert segprof.resolve_ruleset(s, proj) == srx.active_ruleset_bytes()
        # rung 3: system default
        segprof.update_profile(glob.id, is_system_default=True)
        assert segprof.resolve_profile(s, proj).id == glob.id
        # rung 2: customer default beats system default
        customer_service.set_customer_defaults(s, ca.id, segmentation_profile_id=str(mine.id))
        assert segprof.resolve_profile(s, proj).id == mine.id
        # rung 1: project override beats both
        segprof.assign_to_project(s, pid, str(glob.id))
        proj = s.get(Project, pid)
        assert segprof.resolve_profile(s, proj).id == glob.id
        # isolation: B's private profile rejected at both attach points
        try:
            customer_service.set_customer_defaults(s, ca.id, segmentation_profile_id=str(other.id))
            raise AssertionError("cross-customer default accepted")
        except ValueError:
            pass
        try:
            segprof.assign_to_project(s, pid, str(other.id))
            raise AssertionError("cross-customer project assign accepted")
        except segprof.SegProfileError:
            pass
        # dangling id -> next rung (no crash)
        segprof.assign_to_project(s, pid, "")
        customer_service.set_customer_defaults(s, ca.id, segmentation_profile_id="")
        segprof.update_profile(glob.id, is_system_default=False)
    print("  resolution chain + isolation ok")


# --- 5. ingest under the resolved profile --------------------------------------
def test_ingest_resolution():
    pid = _mkproject("Ingest proj")
    # SRX off: chain ignored, legacy stamp, legacy split (1 segment — no
    # sentence terminator boundaries inside TEXT besides the final period).
    settings.set_many({"srx_enabled": "0"})
    _, n_off, engine_off = _ingest(pid)
    assert engine_off == "legacy:2", engine_off  # decision 15: splitter version 2
    # SRX on + semicolon profile assigned: 3 segments, stamp = that ruleset's hash.
    prof = segprof.create_profile("Semi ingest", srx_blob=SEMI_SRX)
    with Session(engine) as s:
        segprof.assign_to_project(s, pid, str(prof.id))
    settings.set_many({"srx_enabled": "1"})
    _, n_on, engine_on = _ingest(pid)
    assert n_on == 3, f"expected 3 semicolon segments, got {n_on}"
    want = "srx:" + srx._ruleset_hash(SEMI_SRX.encode("utf-8"))
    assert engine_on == want, f"{engine_on} != {want}"
    assert n_off != n_on  # observably different segmentation
    # profile blob resolvable by hash (export reproduction path) — and the
    # resolver SELF-HEALS when unregistered (a fresh CLI-only process never
    # imports the profile service; export must still find profile blobs). Use a
    # UNIQUE ruleset that exists ONLY as a profile blob (not on disk / active /
    # default), so the profile resolver is the only rung that can find it.
    tilde_srx = SEMI_SRX.replace(";", "~").replace('"Semi"', '"Tilde"')
    uniq = segprof.create_profile("Tilde unique", srx_blob=tilde_srx)
    srx.register_profile_blob_resolver(None)
    assert srx._document_for_hash(srx._ruleset_hash(tilde_srx.encode("utf-8"))) is not None
    assert srx._PROFILE_BLOB_RESOLVER is not None  # re-registered lazily
    segprof.delete_profile(uniq.id)
    # back to off: legacy again (sacred gate)
    settings.set_many({"srx_enabled": "0"})
    _, _, engine_back = _ingest(pid)
    assert engine_back == "legacy:2"
    with Session(engine) as s:
        segprof.assign_to_project(s, pid, "")
    segprof.delete_profile(prof.id)
    print("  ingest under resolved profile + stamp ok")


# --- 6. web routes --------------------------------------------------------------
def test_web_routes():
    # create (multipart, with an uploaded ruleset)
    r = client.post(
        "/settings/segprofiles",
        data={
            "name": "Web prof",
            "description": "via web",
            "customer_id": "",
            "is_system_default": "",
        },
        files={"srx_upload": ("semi.srx", SEMI_SRX.encode("utf-8"), "application/xml")},
    )
    assert r.status_code == 303 and "srx_msg=" in r.headers["location"], r.headers
    prof = segprof.get_by_name("Web prof")
    assert prof is not None and prof.srx_blob
    # invalid upload rejected
    r = client.post(
        "/settings/segprofiles",
        data={"name": "Bad prof"},
        files={"srx_upload": ("bad.srx", b"<srx>nope", "application/xml")},
    )
    assert "srx_error=" in r.headers["location"]
    assert segprof.get_by_name("Bad prof") is None
    # settings page renders the list
    page = client.get("/settings")
    assert page.status_code == 200 and "Web prof" in page.text
    # clone + default toggle
    r = client.post(f"/settings/segprofiles/{prof.id}/clone", data={"new_name": "Web prof clone"})
    assert "srx_msg=" in r.headers["location"]
    r = client.post(f"/settings/segprofiles/{prof.id}/default", data={"make_default": "1"})
    assert segprof.get_profile(prof.id).is_system_default
    r = client.post(f"/settings/segprofiles/{prof.id}/default", data={"make_default": "0"})
    assert not segprof.get_profile(prof.id).is_system_default
    # project picker POST + in-use delete guard
    pid = _mkproject("Web proj")
    r = client.post(f"/projects/{pid}/segmentation", data={"segmentation_profile_id": str(prof.id)})
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    r = client.post(f"/settings/segprofiles/{prof.id}/delete")
    assert "srx_error=" in r.headers["location"]  # guarded: in use
    assert segprof.get_profile(prof.id) is not None
    # clear the override -> delete passes
    client.post(f"/projects/{pid}/segmentation", data={"segmentation_profile_id": ""})
    r = client.post(f"/settings/segprofiles/{prof.id}/delete")
    assert "srx_msg=" in r.headers["location"]
    assert segprof.get_profile(prof.id) is None
    clone = segprof.get_by_name("Web prof clone")
    segprof.delete_profile(clone.id)
    # project page shows the segmentation row (header reorg, 2026-07:
    # compact fact-strip label, no trailing colon — see app/templates/project.html)
    page = client.get(f"/projects/{pid}")
    assert page.status_code == 200 and "Segmentation" in page.text
    print("  web routes ok")


# --- 7. CLI round-trip ------------------------------------------------------------
def test_cli():
    srx_path = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "cli_semi.srx")
    with open(srx_path, "w", encoding="utf-8") as fh:
        fh.write(SEMI_SRX)
    code, payload, _ = run_json(
        ["segprofile", "create", "CLI prof", "--file", srx_path, "--description", "via cli"]
    )
    assert code == 0 and payload["profile"]["custom"] is True
    pid_prof = payload["profile"]["id"]
    assert "srx_blob" not in json.dumps(payload)  # blob never echoes
    code, payload, _ = run_json(["segprofile", "list"])
    assert code == 0 and any(p["id"] == pid_prof for p in payload["profiles"])
    code, out, _ = run(["segprofile", "show", str(pid_prof)])
    assert code == 0 and "CLI prof" in out and SEMI_SRX[:20] not in out
    code, payload, _ = run_json(["segprofile", "clone", str(pid_prof), "CLI prof v2"])
    assert code == 0
    clone_id = payload["profile"]["id"]
    code, out, _ = run(["segprofile", "set-default", str(pid_prof)])
    assert code == 0 and segprof.get_profile(pid_prof).is_system_default
    code, out, _ = run(["segprofile", "set-default", str(pid_prof), "--clear"])
    assert code == 0 and not segprof.get_profile(pid_prof).is_system_default
    # project create --seg-profile + assign/clear. 2026-07-14: a project requires
    # a customer when one exists — create one and pass --customer-id.
    code, ccout, _ = run_json(["customer", "create", "CLI Seg Cust"])
    assert code == 0, ccout
    _seg_cust = ccout["id"]
    code, out, _ = run(
        [
            "project",
            "create",
            "CLI seg proj",
            "--targets",
            "fr-FR",
            "--seg-profile",
            str(pid_prof),
            "--customer-id",
            str(_seg_cust),
        ]
    )
    assert code == 0, out
    with Session(engine) as s:
        proj = s.exec(select(Project).where(Project.name == "CLI seg proj")).first()
        assert proj.segmentation_profile_id == pid_prof
    code, out, _ = run(["segprofile", "assign", str(proj.id)])
    assert code == 0 and "cleared" in out
    # delete: guard then success
    code, out, _ = run(["segprofile", "delete", str(clone_id), "--yes"])
    assert code == 0
    code, out, _err = run(["segprofile", "delete", "99999", "--yes"])
    assert code != 0
    code, out, _ = run(["segprofile", "delete", str(pid_prof), "--yes"])
    assert code == 0
    print("  cli round-trip ok")


def _login(cl, who, pw):
    csrf_for(cl)
    r = cl.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(cl)
    return r


# --- 8. Day-1a #1: cross-org web routes (clone/default/delete) not-found -----
def test_cross_org_web_routes():
    """Multi-org gate ON: org A's admin gets not-found on org B's profile via
    clone/default/delete -- never enumerable, never mutable/deletable, and org
    B's row survives untouched. An in-org (own) profile still works normally."""
    with Session(engine) as s:
        oa = Organization(name="Seg Org A")
        ob = Organization(name="Seg Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        oa_id, ob_id = oa.id, ob.id
    admin_a = security.create_user("seg-a-admin@x.com", "A Admin", Role.ADMIN, "aa-pass-123456")
    admin_b = security.create_user("seg-b-admin@x.com", "B Admin", Role.ADMIN, "bb-pass-123456")
    with Session(engine) as s:
        for uid, oid in ((admin_a.id, oa_id), (admin_b.id, ob_id)):
            row = s.get(User, uid)
            row.org_id = oid
            s.add(row)
        s.commit()
    # A profile living in org B (created directly, stamped org_id).
    with Session(engine) as s:
        pb = SegmentationProfile(name="Org B seg prof", org_id=ob_id)
        s.add(pb)
        s.commit()
        s.refresh(pb)
        pb_id = pb.id
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    try:
        ca = TestClient(app, follow_redirects=False)
        assert _login(ca, "seg-a-admin@x.com", "aa-pass-123456").status_code == 303
        # clone: not-found redirect, no clone created.
        r = ca.post(f"/settings/segprofiles/{pb_id}/clone", data={"new_name": "Stolen"})
        assert "srx_error=" in r.headers["location"], r.headers
        assert segprof.get_by_name("Stolen") is None
        # default: not-found redirect, org B's profile untouched.
        r = ca.post(f"/settings/segprofiles/{pb_id}/default", data={"make_default": "1"})
        assert "srx_error=" in r.headers["location"], r.headers
        assert segprof.get_profile(pb_id).is_system_default is False
        # delete: not-found redirect, row survives.
        r = ca.post(f"/settings/segprofiles/{pb_id}/delete")
        assert "srx_error=" in r.headers["location"], r.headers
        assert segprof.get_profile(pb_id) is not None
        # sanity: org A's admin CAN clone/default/delete a profile in their OWN org.
        with Session(engine) as s:
            pa = SegmentationProfile(name="Org A seg prof", org_id=oa_id)
            s.add(pa)
            s.commit()
            s.refresh(pa)
            pa_id = pa.id
        r = ca.post(f"/settings/segprofiles/{pa_id}/clone", data={"new_name": "A clone"})
        assert "srx_msg=" in r.headers["location"], r.headers
        assert segprof.get_by_name("A clone") is not None
        r = ca.post(f"/settings/segprofiles/{pa_id}/default", data={"make_default": "1"})
        assert segprof.get_profile(pa_id).is_system_default is True
        r = ca.post(f"/settings/segprofiles/{pa_id}/default", data={"make_default": "0"})
        r = ca.post(f"/settings/segprofiles/{pa_id}/delete")
        assert "srx_msg=" in r.headers["location"], r.headers
        assert segprof.get_profile(pa_id) is None
        segprof.delete_profile(segprof.get_by_name("A clone").id)
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    print("  cross-org web routes: org-B profile not-found via clone/default/delete ok")


# --- 9. Day-1a #4: per-org system-default rung --------------------------------
def test_cross_org_system_default():
    """Multi-org gate ON: two orgs each set their own system default; an org-A
    project resolves ONLY org A's default (never org B's), and org-A's admin
    cannot clear/seize org B's default slot via update_profile."""
    with Session(engine) as s:
        oa = Organization(name="Seg Default Org A")
        ob = Organization(name="Seg Default Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        oa_id, ob_id = oa.id, ob.id
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    try:
        def_a = segprof.create_profile(
            "Default A", srx_blob=SEMI_SRX, is_system_default=True, org_id=oa_id
        )
        def_b = segprof.create_profile("Default B", is_system_default=True, org_id=ob_id)
        # Each org's default flag independently set (creating B's did not clear A's).
        assert segprof.get_profile(def_a.id).is_system_default is True
        assert segprof.get_profile(def_b.id).is_system_default is True
        # A project in org A resolves to org A's default only.
        with Session(engine) as s:
            proj_a = Project(name="Org A def proj", source_locale="en-US", org_id=oa_id)
            s.add(proj_a)
            s.commit()
            s.refresh(proj_a)
            resolved = segprof.resolve_profile(s, proj_a)
            assert resolved is not None and resolved.id == def_a.id, (
                "org A project did not resolve to org A's system default"
            )
            assert resolved.id != def_b.id
        # An org-A admin cannot clear/seize org B's default via update_profile
        # when scoped to org A (org_id=oa_id) -- not-found (None), org B's flag
        # survives.
        result = segprof.update_profile(def_b.id, is_system_default=False, org_id=oa_id)
        assert result is None, "org-A scope reached org-B's profile"
        assert segprof.get_profile(def_b.id).is_system_default is True
        # Cleanup.
        segprof.update_profile(def_a.id, is_system_default=False, org_id=oa_id)
        segprof.update_profile(def_b.id, is_system_default=False, org_id=ob_id)
        segprof.delete_profile(def_a.id, org_id=oa_id)
        segprof.delete_profile(def_b.id, org_id=ob_id)
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    print("  cross-org system default: per-org rung + no cross-org seize ok")


# --- 10. Day-1a #5: --seg-profile success-lie regression ----------------------
def test_seg_profile_create_success_lie():
    """A bad/foreign --seg-profile id must fail the WHOLE create (CLI exit 2, web
    error redirect) with NO project row persisted -- the pre-fix behavior created
    the project first, then failed (CLI) or silently swallowed the error (web)."""
    # CLI: bogus profile id -> exit != 0, no project row.
    code, _out, _err = run(
        ["project", "create", "Seg lie CLI", "--targets", "fr-FR", "--seg-profile", "999999"]
    )
    assert code != 0, "bogus --seg-profile did not fail the CLI create"
    with Session(engine) as s:
        assert s.exec(select(Project).where(Project.name == "Seg lie CLI")).first() is None, (
            "CLI create persisted a project despite the bad seg-profile id"
        )
    # Web: bogus profile id -> error redirect, no project row.
    r = client.post(
        "/projects",
        data={
            "name": "Seg lie web",
            "source_locale": "en-US",
            "target_locales": ["fr-FR"],
            "segmentation_profile_id": "999999",
        },
    )
    assert r.status_code == 303 and "err=" in r.headers["location"], r.headers
    with Session(engine) as s:
        assert s.exec(select(Project).where(Project.name == "Seg lie web")).first() is None, (
            "web create persisted a project despite the bad seg-profile id"
        )
    print("  seg-profile create success-lie: bad id fails whole create, no row ok")


# --- BUG-6: reingest honors the stamped SRX ruleset (not the legacy splitter) --
def test_reingest_uses_srx():
    """reingest_document (broken-ingest repair) must re-split with the SAME resolved
    SRX profile a fresh upload uses -- NOT the legacy splitter. Pre-fix it called
    split_sentences directly, so an SRX-segmented doc (3 semicolon segments) rebuilt
    to the LEGACY split (1 segment), silently scrambling the round-trip. The docstring
    already promised 'identical to a fresh upload'; this pins it."""
    from app.services.ingest import reingest_document

    pid = _mkproject("Reingest proj")
    prof = segprof.create_profile("Semi reingest", srx_blob=SEMI_SRX)
    with Session(engine) as s:
        segprof.assign_to_project(s, pid, str(prof.id))
    settings.set_many({"srx_enabled": "1"})
    try:
        doc_id, n_on, _engine_on = _ingest(pid)
        assert n_on == 3, f"ingest under SRX should give 3 segments, got {n_on}"
        # Repair the doc: reingest must reproduce the SAME 3-segment SRX split.
        assert reingest_document(doc_id) is True, "reingest declined a clean doc"
        with Session(engine) as s:
            n_re = len(
                s.exec(
                    select(Segment).where(
                        Segment.document_id == doc_id, Segment.target_locale == "fr-FR"
                    )
                ).all()
            )
        assert n_re == n_on == 3, (
            f"reingest ignored the stamped SRX ruleset: {n_re} segments "
            f"(legacy would give 1), expected {n_on}"
        )
    finally:
        settings.set_many({"srx_enabled": "0"})
        with Session(engine) as s:
            segprof.assign_to_project(s, pid, "")
        segprof.delete_profile(prof.id)
    print("  reingest uses the resolved SRX profile (3 SRX segments, not legacy 1) ok")


def test_reingest_restamps_seg_engine():
    """Reviewer F1 (config drift): a doc ingested LEGACY (srx off), then repaired
    AFTER SRX+profile were enabled, must come out of reingest with (a) the SRX split
    AND (b) a re-stamped seg_engine/seg_locale IDENTICAL to what a fresh ingest under
    the same settings stamps. Pre-fix the split changed but the stamp stayed
    'legacy', so export.paragraph_texts_for (which re-splits by the STAMP) counted 1
    sentence for a 3-segment unit -- 2 of 3 targets silently dropped at export."""
    from app.services.export import paragraph_texts_for
    from app.services.ingest import reingest_document

    pid = _mkproject("Restamp proj")
    # (1) ingest with SRX OFF -> legacy stamp, 1 segment.
    settings.set_many({"srx_enabled": "0"})
    doc_id, n_off, engine_off = _ingest(pid)
    assert n_off == 1 and engine_off == "legacy:2", (n_off, engine_off)
    # (2) settings drift: enable SRX + assign the semicolon profile.
    prof = segprof.create_profile("Semi restamp", srx_blob=SEMI_SRX)
    with Session(engine) as s:
        segprof.assign_to_project(s, pid, str(prof.id))
    settings.set_many({"srx_enabled": "1"})
    try:
        # (3) repair: SRX split AND the stamp both match a fresh ingest.
        assert reingest_document(doc_id) is True, "reingest declined a clean doc"
        _fresh_id, n_fresh, engine_fresh = _ingest(pid)  # the reference stamp
        assert n_fresh == 3 and engine_fresh.startswith("srx:"), (n_fresh, engine_fresh)
        with Session(engine) as s:
            doc = s.get(Document, doc_id)
            skel = doc.skeleton or {}
            segs = s.exec(
                select(Segment)
                .where(Segment.document_id == doc_id, Segment.target_locale == "fr-FR")
                .order_by(Segment.order_index)
            ).all()
            assert len(segs) == 3, ("reingest must SRX-split", len(segs))
            assert skel.get("seg_engine") == engine_fresh, (
                "reingest left a stale seg_engine stamp (export would re-split by it and misalign)",
                skel.get("seg_engine"),
                engine_fresh,
            )
            assert skel.get("seg_locale") == "en-US", skel.get("seg_locale")
            # (4) export alignment holds: mark all 3 targets and rebuild the
            # paragraph mapping -- ALL 3 translated sentences must land in the
            # single source unit (a stale 'legacy' stamp keeps only the first).
            for k, sg in enumerate(segs):
                sg.target_text = f"T{k}"
                s.add(sg)
            s.commit()
            doc = s.get(Document, doc_id)
            mapping = paragraph_texts_for(s, doc, "fr-FR")
            joined = " ".join(mapping.values())
            for k in range(3):
                assert f"T{k}" in joined, (
                    f"target T{k} dropped at export (stamp/split misalignment)",
                    mapping,
                )
    finally:
        settings.set_many({"srx_enabled": "0"})
        with Session(engine) as s:
            segprof.assign_to_project(s, pid, "")
        segprof.delete_profile(prof.id)
    print(
        "  reingest re-stamps seg_engine/seg_locale == fresh ingest; export "
        "alignment holds (all 3 targets land) ok"
    )


# --- #451: reingest rebuilds under the project's target locales ----------------
def _locales_of(doc_id):
    """{locale: [order_index, ...]} for a document's segments."""
    with Session(engine) as s:
        out = {}
        for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            out.setdefault(sg.target_locale, []).append(sg.order_index)
    return {loc: sorted(orders) for loc, orders in out.items()}


def test_reingest_zero_segment_doc_uses_project_locales():
    """#451: a document whose first parse produced NO segments -- the exact case this
    repair exists for -- must be rebuilt under the PROJECT's target locales. Pre-fix
    reingest_document read its locales from the segments it found, so an empty segment
    list fell through to the placeholder locale and the repaired document still had
    nothing translatable or exportable for fr-FR or de-DE."""
    from app.ports import blobstore
    from app.services.ingest import ingest_document, reingest_document

    pid = _mkproject("Zero-seg repair")  # targets fr-FR
    with Session(engine) as s:
        s.add(TargetLocale(project_id=pid, locale="de-DE"))
        s.commit()
        proj = s.get(Project, pid)
        # A parse that yields no units (blank text) -> a committed doc with zero segments.
        # Uploads refuse this since #573; allow_empty builds the zero-unit document an
        # older install (or a zero-region image) can still hold, which is what repair fixes.
        doc = ingest_document(
            s, proj, "blank.txt", b"\n   \n", TransMode.SEGMENTED, allow_empty=True
        )
        doc_id = doc.id
        key = blobstore.document_key(doc)
        assert s.exec(select(Segment).where(Segment.document_id == doc_id)).all() == [], (
            "a blank upload should seed no segments"
        )
        # The repair re-parses the STORED bytes, so the upload it works on is a parsable
        # file (a broken first parse is what the repair is for).
        blobstore.get().put(key, TEXT.encode("utf-8"))
    assert reingest_document(doc_id) is True, "reingest declined a zero-segment doc"
    got = _locales_of(doc_id)
    assert set(got) == {"fr-FR", "de-DE"}, (
        "a zero-segment repair must rebuild under the project's target locales",
        got,
    )
    for loc, orders in got.items():
        assert orders and orders == list(range(len(orders))), (loc, orders)
        with Session(engine) as s:
            texts = s.exec(
                select(Segment.source_text)
                .where(Segment.document_id == doc_id, Segment.target_locale == loc)
                .order_by(Segment.order_index)
            ).all()
        assert all(t.strip() for t in texts), (loc, texts)
    print("  reingest of a zero-segment doc rebuilds under the project's locales ok")


def test_reingest_unions_project_and_existing_locales():
    """#451 (merge rule): a repair takes the project's target locales UNION the locales
    already on the broken segments, in a stable order with no duplicates. Both
    directions matter: a locale the project ADDED after ingest must appear on the
    repaired document (pre-fix, the segments decided alone), and a locale the project
    DROPPED must survive the repair rather than have its whole segment set deleted."""
    from app.services.ingest import ingest_document, reingest_document

    pid = _mkproject("Locale-union repair")  # targets fr-FR
    with Session(engine) as s:
        proj = s.get(Project, pid)
        doc = ingest_document(s, proj, "sample.txt", TEXT.encode("utf-8"), TransMode.SEGMENTED)
        doc_id = doc.id
    counts = {loc: len(orders) for loc, orders in _locales_of(doc_id).items()}
    assert set(counts) == {"fr-FR"}, counts
    # (a) the project gains a locale AFTER ingest: the repair must pick it up.
    with Session(engine) as s:
        s.add(TargetLocale(project_id=pid, locale="de-DE"))
        s.commit()
    assert reingest_document(doc_id) is True, "reingest declined a clean doc"
    assert set(_locales_of(doc_id)) == {"fr-FR", "de-DE"}, (
        "a repair must seed the project's target locales, not only the existing ones",
        _locales_of(doc_id),
    )
    # (b) the project drops fr-FR: the document's own fr-FR segments keep that locale.
    with Session(engine) as s:
        for row in s.exec(
            select(TargetLocale).where(
                TargetLocale.project_id == pid, TargetLocale.locale == "fr-FR"
            )
        ).all():
            s.delete(row)
        s.commit()
    assert reingest_document(doc_id) is True, "reingest declined a clean doc"
    after = _locales_of(doc_id)
    assert set(after) == {"fr-FR", "de-DE"}, (
        "a repair must keep a locale the segments carry even if the project dropped it",
        after,
    )
    # One segment set per locale (never a duplicate), same segmentation every repair:
    # order_index contiguous from 0 and the per-locale counts match the first ingest.
    assert {loc: len(orders) for loc, orders in after.items()} == {
        "fr-FR": counts["fr-FR"],
        "de-DE": counts["fr-FR"],
    }, (after, counts)
    for orders in after.values():
        assert orders == list(range(len(orders))), orders
    print("  reingest locale set = project targets UNION existing segment locales ok")


def test_reingest_drops_placeholder_once_a_real_locale_exists():
    """#451 (gate): a document ingested while its project had NO target locale carries the
    placeholder locale. After the project gains fr-FR, a repair must rebuild under fr-FR
    only, as a fresh upload would; the union rule must not keep a placeholder segment set
    next to the real one."""
    from app.services.ingest import ingest_document, reingest_document

    pid = _mkproject("Placeholder repair")  # targets fr-FR
    with Session(engine) as s:
        for row in s.exec(select(TargetLocale).where(TargetLocale.project_id == pid)).all():
            s.delete(row)
        s.commit()
        proj = s.get(Project, pid)
        doc = ingest_document(s, proj, "sample.txt", TEXT.encode("utf-8"), TransMode.SEGMENTED)
        doc_id = doc.id
    assert set(_locales_of(doc_id)) == {"—"}, _locales_of(doc_id)
    with Session(engine) as s:
        s.add(TargetLocale(project_id=pid, locale="fr-FR"))
        s.commit()
    assert reingest_document(doc_id) is True, "reingest declined a clean doc"
    assert set(_locales_of(doc_id)) == {"fr-FR"}, (
        "a repair must not keep placeholder segments once a real locale exists",
        _locales_of(doc_id),
    )
    print("  reingest drops the placeholder locale once the project has a real one ok")


if __name__ == "__main__":
    test_crud()
    test_legacy_migration()
    test_chain_and_isolation()
    test_ingest_resolution()
    test_reingest_uses_srx()
    test_reingest_restamps_seg_engine()
    test_reingest_zero_segment_doc_uses_project_locales()
    test_reingest_unions_project_and_existing_locales()
    test_reingest_drops_placeholder_once_a_real_locale_exists()
    test_web_routes()
    test_cli()
    test_cross_org_web_routes()
    test_cross_org_system_default()
    test_seg_profile_create_success_lie()
    print("SEG PROFILES OK")
