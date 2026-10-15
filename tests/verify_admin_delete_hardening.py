"""Admin delete-hardening + rename parity verification (SPEC-rename-delete, Branch 3).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider).
ZERO live calls. ASCII-only output. Final line: "ADMIN DELETE HARDENING OK".

Covers Branch 3 of SPEC-rename-delete:

  1. EngineProfile.delete_profile FAILS CLOSED per referrer column (one sub-check per
     column): plant an engine:<id> ref in each of
        Project.default_provider, Customer.default_provider,
        ProjectPairProvider.provider, RoutingRule.provider,
        WorkflowStep.provider / qe_provider / fallback_provider,
        ProjectTemplate.default_provider, and the global default_provider Setting
     -> engines.delete_profile raises EngineInUseError (+ reference_count key > 0);
     clear the ref -> delete succeeds. (The global-Setting referrer is BEYOND the
     spec's 8-column list — discovered by grep: the Setup AI picker offers engine:<id>
     as the install default, so deleting it would orphan config.DEFAULT_PROVIDER.)

  2. SegmentationProfile.delete_profile fails closed AT THE SERVICE SEAM: calling the
     service delete DIRECTLY (no route guard) with a live project reference now RAISES
     (the pre-fix bug: a bare delete that orphaned the ref). Pre-fix proof = an inline
     replica of the old bare-delete, shown to orphan a live reference.

  3. SegmentationProfile rename on all THREE surfaces (service / CLI / web / api) +
     the app-level dup-check (case-fold behavior unchanged) + audit old->new detail.

  4. ContentType rename audit detail carries old->new (pre-fix: detail was empty). The
     positive assertion IS the regression guard — it fails on the pre-fix code path.

  5. ProjectTemplate rename: service rename_template + CLI `template rename` + dup-check.

  6. RBAC: a non-admin (linguist) is BLOCKED on web engine-delete + segprofile-delete
     (the tightened admin gate), and admin succeeds; /api segprofile rename is admin-only
     (linguist token 403, admin token 200).

Adversarial-review follow-up (review-admin-rd/tests/probe_adversarial_rd.py confirmed
3 defects against the above; these close them):

  7. FIX 1 (HIGH): engine delete fails closed on the global default_fallback_provider
     Setting (settings.py's sibling of default_provider, consumed by translate.py for
     EVERY ProviderSpec.fallback_provider) -- reference_count previously checked only
     default_provider, so deleting an engine that was ONLY the global fallback silently
     orphaned it.

  8. FIX 2 (MED): a non-canonical-but-LIVE engine:<id> ref (zero-padded "engine:05",
     interior-whitespace "engine: 5 ") evades an EXACT string match but still resolves
     live via providers.get_provider's int()-parse -- reference_count now matches by
     PARSED id (like providers.py does), not literal text. A prefix collision
     (engine:<id>9) must still NOT false-positive. Also: the one provider-string WRITE
     seam in this branch's files (setup-ai's default_provider save) now canonicalizes
     on write (defense in depth; the reader-side fix alone is sufficient).

  9. FIX 3 (LOW): segmentation_profiles.update_profile(name="   ") used to be a silent
     no-op "success" (create_profile/rename_template both refuse a blank name) -- it now
     raises SegProfileError like its neighbors, on both the service and the CLI surface,
     and writes no old->old audit row.

  10. TOCTOU (cheap fold-in): engines.delete_profile / segprofiles.delete_profile now run
      their reference count in the SAME session/transaction as the delete, instead of a
      separate reference_count()/in_use_count() call opening its own session.
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

init_env("loctms_adminrd_")

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Customer,
    Project,
    ProjectPairProvider,
    ProjectTemplate,
    Role,
    RoutingRule,
    SegmentationProfile,
    Workflow,
    WorkflowStep,
)
from app.services import (
    apikeys,
    engines,
    security,
    settings,
)
from app.services import (
    segmentation_profiles as segprof,
)
from app.services import (
    templates as tpl_svc,
)
from tests._csrf import csrf_for

init_db()
# Audit rows are a no-op unless the global gate is on; the rename-detail checks need
# them written.
settings.set_many({"audit_enabled": "1"})
settings.invalidate_cache()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# --- CLI in-process runner (mirrors the neighbor verify suites) --------------
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


_NAME_SEQ = 0


def _uniq(prefix):
    global _NAME_SEQ
    _NAME_SEQ += 1
    return f"{prefix}-{_NAME_SEQ}"


def _fresh_engine():
    """A brand-new, unreferenced local engine; returns its id (+ engine:<id> ref)."""
    prof = engines.create_profile(_uniq("Eng"), "local", base_url="http://x", actor="test")
    return prof.id, f"engine:{prof.id}"


def _latest_detail(action):
    with Session(engine) as s:
        row = s.exec(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.desc()).limit(1)
        ).first()
    return row.detail if row else None


# ============================================================================
# 1. Engine delete fails closed for EVERY referrer column
# ============================================================================
def test_engine_delete_fails_closed_per_column():
    # Shared parents for the FK-bearing referrer rows.
    with Session(engine) as s:
        base_proj = Project(name=_uniq("base-proj"), source_locale="en-US")
        base_cust = Customer(name=_uniq("base-cust"))
        base_wf = Workflow(name=_uniq("base-wf"))
        s.add(base_proj)
        s.add(base_cust)
        s.add(base_wf)
        s.commit()
        s.refresh(base_proj)
        s.refresh(base_wf)
        base_pid, base_wid = base_proj.id, base_wf.id

    def _check(label, key, plant, clear):
        """plant(eid, ref) creates the referrer; clear() removes it. Assert the
        service refuses (EngineInUseError) with the right count key, then that a
        cleared engine deletes cleanly."""
        eid, ref = _fresh_engine()
        row_id = plant(eid, ref)
        counts = engines.reference_count(eid)
        if counts[key] < 1:
            _fail(f"[{label}] reference_count[{key}] should be >=1, got {counts}")
        if counts["total"] < 1:
            _fail(f"[{label}] total should be >=1, got {counts}")
        try:
            engines.delete_profile(eid)
        except engines.EngineInUseError:
            pass
        else:
            _fail(f"[{label}] delete_profile must FAIL CLOSED while referenced")
        # Engine row must still exist after the refusal (no partial delete).
        if engines.get_profile(eid) is None:
            _fail(f"[{label}] engine was deleted despite the refusal")
        clear(row_id)
        if engines.reference_count(eid)["total"] != 0:
            _fail(f"[{label}] ref not cleared: {engines.reference_count(eid)}")
        if engines.delete_profile(eid) is not True:
            _fail(f"[{label}] delete must succeed once unreferenced")
        if engines.get_profile(eid) is not None:
            _fail(f"[{label}] engine survived a clean delete")

    # --- Project.default_provider ---
    def _plant_project(eid, ref):
        with Session(engine) as s:
            p = Project(name=_uniq("p"), source_locale="en-US", default_provider=ref)
            s.add(p)
            s.commit()
            s.refresh(p)
            return ("project", p.id)

    def _clear_project(row):
        with Session(engine) as s:
            p = s.get(Project, row[1])
            p.default_provider = "claude"
            s.add(p)
            s.commit()

    _check("Project.default_provider", "projects", _plant_project, _clear_project)

    # --- Customer.default_provider ---
    def _plant_customer(eid, ref):
        with Session(engine) as s:
            c = Customer(name=_uniq("c"), default_provider=ref)
            s.add(c)
            s.commit()
            s.refresh(c)
            return ("customer", c.id)

    def _clear_customer(row):
        with Session(engine) as s:
            c = s.get(Customer, row[1])
            c.default_provider = None
            s.add(c)
            s.commit()

    _check("Customer.default_provider", "customer_defaults", _plant_customer, _clear_customer)

    # --- ProjectPairProvider.provider ---
    def _plant_pair(eid, ref):
        with Session(engine) as s:
            pp = ProjectPairProvider(
                project_id=base_pid, source_locale="en-US", target_locale="fr-FR", provider=ref
            )
            s.add(pp)
            s.commit()
            s.refresh(pp)
            return ("pair", pp.id)

    def _clear_pair(row):
        with Session(engine) as s:
            s.delete(s.get(ProjectPairProvider, row[1]))
            s.commit()

    _check("ProjectPairProvider.provider", "pair_providers", _plant_pair, _clear_pair)

    # --- RoutingRule.provider ---
    def _plant_rule(eid, ref):
        with Session(engine) as s:
            rr = RoutingRule(provider=ref)
            s.add(rr)
            s.commit()
            s.refresh(rr)
            return ("rule", rr.id)

    def _clear_rule(row):
        with Session(engine) as s:
            s.delete(s.get(RoutingRule, row[1]))
            s.commit()

    _check("RoutingRule.provider", "routing_rules", _plant_rule, _clear_rule)

    # --- WorkflowStep.provider / qe_provider / fallback_provider ---
    def _mk_step(**cols):
        def _plant(eid, ref):
            with Session(engine) as s:
                st = WorkflowStep(workflow_id=base_wid, **dict.fromkeys(cols, ref))
                s.add(st)
                s.commit()
                s.refresh(st)
                return ("step", st.id)

        return _plant

    def _clear_step(row):
        with Session(engine) as s:
            s.delete(s.get(WorkflowStep, row[1]))
            s.commit()

    _check("WorkflowStep.provider", "workflow_step_provider", _mk_step(provider=1), _clear_step)
    _check(
        "WorkflowStep.qe_provider",
        "workflow_step_qe_provider",
        _mk_step(qe_provider=1),
        _clear_step,
    )
    # QE auto-fix (Train B): the post-editor ref is live config like the other three.
    _check(
        "WorkflowStep.qe_autofix_provider",
        "workflow_step_autofix_provider",
        _mk_step(qe_autofix_provider=1),
        _clear_step,
    )
    _check(
        "WorkflowStep.fallback_provider",
        "workflow_step_fallback",
        _mk_step(fallback_provider=1),
        _clear_step,
    )

    # --- ProjectTemplate.default_provider ---
    def _plant_template(eid, ref):
        with Session(engine) as s:
            t = ProjectTemplate(name=_uniq("tpl"), default_provider=ref)
            s.add(t)
            s.commit()
            s.refresh(t)
            return ("tpl", t.id)

    def _clear_template(row):
        with Session(engine) as s:
            s.delete(s.get(ProjectTemplate, row[1]))
            s.commit()

    _check("ProjectTemplate.default_provider", "templates", _plant_template, _clear_template)

    # --- global default_provider Setting (beyond the 8 columns) ---
    def _plant_global(eid, ref):
        settings.set_many({"default_provider": ref})
        settings.invalidate_cache()
        return ("global", None)

    def _clear_global(row):
        settings.set_many({"default_provider": "claude"})
        settings.invalidate_cache()

    _check("Setting default_provider (global)", "global_default", _plant_global, _clear_global)

    # Case-insensitive match: an UPPER-cased stored ref must still be counted (the
    # provider resolver lower-cases at run time, so the count must not miss it).
    eid, ref = _fresh_engine()
    with Session(engine) as s:
        p = Project(name=_uniq("upper"), source_locale="en-US", default_provider=ref.upper())
        s.add(p)
        s.commit()
        s.refresh(p)
        upper_pid = p.id
    if engines.reference_count(eid)["projects"] != 1:
        _fail("case-insensitive referrer match failed (ENGINE:<id> not counted)")
    with Session(engine) as s:
        s.delete(s.get(Project, upper_pid))
        s.commit()
    engines.delete_profile(eid)

    print(
        "  engine delete fails closed per referrer column (8 cols + global "
        "setting + case-insensitive): OK"
    )


# ============================================================================
# 2. SegmentationProfile fails closed AT THE SERVICE (the pre-fix bug)
# ============================================================================
def _pre_fix_bare_delete(pid):
    """Inline replica of the PRE-FIX segmentation_profiles.delete_profile: a bare
    hard delete with NO in_use guard ("callers should guard"). Used only to PROVE
    the pre-fix path did NOT fail closed gracefully."""
    with Session(engine) as s:
        prof = s.get(SegmentationProfile, pid)
        s.delete(prof)
        s.commit()


def test_segprofile_service_fails_closed():
    # A profile referenced by a live project.
    prof_a = segprof.create_profile(_uniq("SegA"), actor="test")
    with Session(engine) as s:
        p = Project(name=_uniq("segproj"), source_locale="en-US", segmentation_profile_id=prof_a.id)
        s.add(p)
        s.commit()
        s.refresh(p)
        proj_id = p.id
    assert segprof.in_use_count(prof_a.id)["total"] == 1

    # THE FIX: the SERVICE itself refuses with a CLEAN, itemized SegProfileError (no
    # route guard involved) — this is the exact call the pre-fix comment said
    # "callers should guard", so a direct service caller could bypass the guard.
    try:
        segprof.delete_profile(prof_a.id)
    except segprof.SegProfileError:
        pass
    else:
        _fail("segprofile.delete_profile must FAIL CLOSED at the service seam")
    if segprof.get_profile(prof_a.id) is None:
        _fail("segprofile deleted despite the refusal")

    # PRE-FIX PROOF: deleting the SAME live-referenced profile the OLD way (a bare,
    # unguarded delete) does NOT fail closed gracefully — under the global FK
    # enforcement it raises a raw IntegrityError (a 500-class error), exactly the
    # ugliness the new service-level guard prevents by refusing cleanly FIRST.
    try:
        _pre_fix_bare_delete(prof_a.id)
    except IntegrityError:
        pass
    else:
        _fail("pre-fix bare delete unexpectedly succeeded on a referenced profile")
    if segprof.get_profile(prof_a.id) is None:
        _fail("pre-fix bare delete should have left the profile intact (rollback)")

    # Clear the ref -> the fixed service now deletes cleanly.
    with Session(engine) as s:
        p = s.get(Project, proj_id)
        p.segmentation_profile_id = None
        s.add(p)
        s.commit()
    if segprof.delete_profile(prof_a.id) is not True:
        _fail("segprofile delete must succeed once unreferenced")
    print("  segprofile fails closed at the SERVICE (pre-fix bare delete = raw 500): OK")


# ============================================================================
# 3. SegmentationProfile rename: service + CLI + api + web + dup-check + audit
# ============================================================================
def test_segprofile_rename_surfaces():
    # service
    prof = segprof.create_profile(_uniq("RenSvc"), actor="test")
    renamed = segprof.update_profile(prof.id, name="Ren Svc New")
    if renamed is None or renamed.name != "Ren Svc New":
        _fail("service update_profile(name=) did not rename")

    # dup-check (case-fold behavior unchanged: name_taken is exact via get_by_name).
    other = segprof.create_profile(_uniq("RenDup"), actor="test")
    try:
        segprof.update_profile(other.id, name="Ren Svc New")
    except segprof.SegProfileError:
        pass
    else:
        _fail("segprofile rename must reject a duplicate name")

    # CLI + audit old->new
    cli_prof = segprof.create_profile("Seg CLI Old", actor="test")
    code, payload, err = run_json(["segprofile", "rename", str(cli_prof.id), "Seg CLI New"])
    if code != 0 or payload.get("profile", {}).get("name") != "Seg CLI New":
        _fail(f"CLI segprofile rename failed: code={code} err={err}")
    det = _latest_detail("segprofile.rename")
    if not det or "Seg CLI Old" not in det or "Seg CLI New" not in det:
        _fail(f"segprofile.rename audit detail missing old->new: {det!r}")

    # /api rename (admin-gated) exercised in the RBAC test below; here confirm the
    # web route renames + audits.
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        web_prof = segprof.create_profile("Seg Web Old", actor="test")
        admin = security.create_user(
            _uniq("segadmin") + "@x.com", "Seg Admin", Role.ADMIN, "seg-admin-pw-123"
        )
        cl = TestClient(app, follow_redirects=False)
        csrf_for(cl)
        r = cl.post(
            "/auth/login", data={"email": admin.email, "password": "seg-admin-pw-123", "next": "/"}
        )
        assert r.status_code == 303, ("login", r.status_code)
        csrf_for(cl)
        r = cl.post(f"/settings/segprofiles/{web_prof.id}/rename", data={"name": "Seg Web New"})
        assert "srx_msg=" in r.headers["location"], r.headers
        if segprof.get_profile(web_prof.id).name != "Seg Web New":
            _fail("web segprofile rename did not persist")
        det = _latest_detail("segprofile.rename")
        if not det or "Seg Web Old" not in det or "Seg Web New" not in det:
            _fail(f"web segprofile.rename detail missing old->new: {det!r}")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  segprofile rename on service + CLI + web (+ dup-check + audit old->new): OK")


# ============================================================================
# 4. ContentType rename audit detail carries old->new (pre-fix: empty)
# ============================================================================
def test_content_type_rename_audit_detail():
    code, payload, err = run_json(["content-type", "create", "CT Rename Old"])
    if code != 0:
        _fail(f"content-type create failed: {err}")
    ctid = payload["id"]
    code, _payload, err = run_json(["content-type", "rename", str(ctid), "CT Rename New"])
    if code != 0:
        _fail(f"content-type rename failed: {err}")
    det = _latest_detail("content_type.rename")
    # The pre-fix code wrote NO detail (empty) — this positive assertion is the
    # regression guard (it fails on the pre-fix code path).
    if not det or "CT Rename Old" not in det or "CT Rename New" not in det:
        _fail(f"content_type.rename audit detail missing old->new: {det!r}")
    print("  content-type rename audit detail carries old->new: OK")


# ============================================================================
# 5. ProjectTemplate rename: service + CLI + dup-check
# ============================================================================
def test_template_rename():
    # A project to snapshot.
    with Session(engine) as s:
        proj = Project(name=_uniq("tplproj"), source_locale="en-US")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        proj_id = proj.id

    # service rename
    with Session(engine) as s:
        t = tpl_svc.save_as_template(s, proj_id, "Tpl Svc Old", actor="test")
        tid = t.id
    with Session(engine) as s:
        renamed = tpl_svc.rename_template(s, tid, "Tpl Svc New")
        if renamed.name != "Tpl Svc New":
            _fail("service rename_template did not rename")

    # dup-check (exact-match via get_by_name — behavior unchanged from save path)
    with Session(engine) as s:
        t2 = tpl_svc.save_as_template(s, proj_id, "Tpl Dup", actor="test")
        t2_id = t2.id
    with Session(engine) as s:
        try:
            tpl_svc.rename_template(s, t2_id, "Tpl Svc New")
        except tpl_svc.TemplateError:
            pass
        else:
            _fail("template rename must reject a duplicate name")

    # CLI rename + audit old->new
    with Session(engine) as s:
        t3 = tpl_svc.save_as_template(s, proj_id, "Tpl CLI Old", actor="test")
        t3_id = t3.id
    code, payload, err = run_json(["template", "rename", str(t3_id), "Tpl CLI New"])
    if code != 0 or payload.get("template", {}).get("name") != "Tpl CLI New":
        _fail(f"CLI template rename failed: code={code} err={err}")
    det = _latest_detail("template.rename")
    if not det or "Tpl CLI Old" not in det or "Tpl CLI New" not in det:
        _fail(f"template.rename audit detail missing old->new: {det!r}")
    print("  template rename on service + CLI (+ dup-check + audit old->new): OK")


# ============================================================================
# 6. RBAC: non-admin blocked from web engine/segprofile delete; api rename admin-only
# ============================================================================
def _key(user):
    _row, raw = apikeys.create_key(user.id, "adminrd", scopes="api docs")
    return raw


def test_rbac_non_admin_blocked():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        admin = security.create_user(
            _uniq("rbacadmin") + "@x.com", "RBAC Admin", Role.ADMIN, "rbac-admin-pw-123"
        )
        ling = security.create_user(
            _uniq("rbacling") + "@x.com", "RBAC Ling", Role.LINGUIST, "rbac-ling-pw-123"
        )

        # --- WEB: linguist blocked, admin allowed (engine + segprofile delete) ---
        eng_id, _ = _fresh_engine()  # unreferenced -> admin can delete it
        seg = segprof.create_profile(_uniq("RbacSeg"), actor="test")

        # linguist session
        cl = TestClient(app, follow_redirects=False)
        csrf_for(cl)
        r = cl.post(
            "/auth/login", data={"email": ling.email, "password": "rbac-ling-pw-123", "next": "/"}
        )
        assert r.status_code == 303, ("linguist login", r.status_code)
        csrf_for(cl)
        r = cl.post(f"/setup-ai/engines/{eng_id}/delete")
        if r.status_code != 403:
            _fail(f"linguist must be 403 on engine delete, got {r.status_code}")
        r = cl.post(f"/settings/segprofiles/{seg.id}/delete")
        if r.status_code != 403:
            _fail(f"linguist must be 403 on segprofile delete, got {r.status_code}")
        # both survive the blocked attempts
        if engines.get_profile(eng_id) is None or segprof.get_profile(seg.id) is None:
            _fail("a blocked non-admin delete still mutated data")

        # admin session -> deletes succeed
        ca = TestClient(app, follow_redirects=False)
        csrf_for(ca)
        r = ca.post(
            "/auth/login", data={"email": admin.email, "password": "rbac-admin-pw-123", "next": "/"}
        )
        assert r.status_code == 303, ("admin login", r.status_code)
        csrf_for(ca)
        r = ca.post(f"/setup-ai/engines/{eng_id}/delete")
        assert r.status_code in (302, 303), ("admin engine delete", r.status_code)
        if engines.get_profile(eng_id) is not None:
            _fail("admin engine delete did not remove the engine")
        r = ca.post(f"/settings/segprofiles/{seg.id}/delete")
        assert "srx_msg=" in r.headers["location"], r.headers
        if segprof.get_profile(seg.id) is not None:
            _fail("admin segprofile delete did not remove the profile")

        # --- /api: segprofile rename is admin-only ---
        api_seg = segprof.create_profile("Api Seg Old", actor="test")
        ah = {"X-Loc-Token": _key(admin)}
        lh = {"X-Loc-Token": _key(ling)}
        capi = TestClient(app)
        r = capi.post(f"/api/segprofiles/{api_seg.id}/rename", headers=lh, json={"name": "hijack"})
        if r.status_code != 403:
            _fail(f"/api segprofile rename must 403 for linguist, got {r.status_code}")
        r = capi.post(
            f"/api/segprofiles/{api_seg.id}/rename", headers=ah, json={"name": "Api Seg New"}
        )
        if r.status_code != 200 or r.json()["profile"]["name"] != "Api Seg New":
            _fail(f"/api segprofile rename must succeed for admin: {r.status_code} {r.text}")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  RBAC: non-admin blocked on web engine/segprofile delete; api rename admin-only: OK")


# ============================================================================
# 7. FIX 1 (HIGH): engine delete fails closed on the global
#    default_fallback_provider Setting (the sibling reference_count previously
#    missed -- see app/services/settings.py DEFAULTS + translate.py's use of
#    config.DEFAULT_FALLBACK_PROVIDER for every ProviderSpec.fallback_provider).
# ============================================================================
def test_engine_delete_global_fallback_referrer():
    eid, ref = _fresh_engine()
    settings.set_many({"default_fallback_provider": ref})
    settings.invalidate_cache()
    try:
        counts = engines.reference_count(eid)
        if counts["global_fallback"] < 1:
            _fail(f"reference_count['global_fallback'] should be >=1, got {counts}")
        if counts["total"] < 1:
            _fail(f"total should be >=1 while the global fallback refers to it: {counts}")
        try:
            engines.delete_profile(eid)
        except engines.EngineInUseError as exc:
            if "fallback" not in str(exc).lower():
                _fail(f"EngineInUseError message should mention the fallback provider: {exc}")
        else:
            _fail("delete_profile must FAIL CLOSED while default_fallback_provider refers to it")
        if engines.get_profile(eid) is None:
            _fail("engine deleted despite the global-fallback refusal")
    finally:
        settings.set_many({"default_fallback_provider": ""})
        settings.invalidate_cache()
    if engines.reference_count(eid)["total"] != 0:
        _fail(f"fallback ref not cleared: {engines.reference_count(eid)}")
    if engines.delete_profile(eid) is not True:
        _fail("delete must succeed once the global fallback ref is cleared")
    print(
        "  engine delete fails closed on the global default_fallback_provider Setting (FIX 1): OK"
    )


# ============================================================================
# 8. FIX 2 (MED): non-canonical-but-LIVE engine:<id> refs (zero-padded,
#    interior-whitespace) still resolve live via providers.get_provider's
#    int()-parse and must still be COUNTED -- reference_count now matches by
#    parsed id, not literal string equality. A prefix collision must still NOT
#    false-positive. Also exercises the FIX 2a write-seam canonicalization.
# ============================================================================
def test_engine_delete_noncanonical_ref_counted():
    from app.services import providers

    def _plant_and_assert(label, shaped_tmpl):
        eid, _ref = _fresh_engine()
        shaped = shaped_tmpl.format(id=eid)
        # Precondition: this shaped ref really does resolve LIVE via the same
        # parse providers.get_provider/provider_is_live use -- otherwise this
        # probe would be testing nothing.
        if not providers.provider_is_live(shaped):
            _fail(f"[{label}] precondition failed: {shaped!r} does not resolve live")
        with Session(engine) as s:
            p = Project(name=_uniq("noncanon"), source_locale="en-US", default_provider=shaped)
            s.add(p)
            s.commit()
            s.refresh(p)
            proj_id = p.id
        try:
            counts = engines.reference_count(eid)
            if counts["total"] < 1 or counts["projects"] < 1:
                _fail(f"[{label}] reference_count missed live ref {shaped!r}: {counts}")
            try:
                engines.delete_profile(eid)
            except engines.EngineInUseError:
                pass
            else:
                _fail(f"[{label}] delete_profile must FAIL CLOSED on live ref {shaped!r}")
        finally:
            with Session(engine) as s:
                row = s.get(Project, proj_id)
                if row:
                    s.delete(row)
                    s.commit()
        if engines.delete_profile(eid) is not True:
            _fail(f"[{label}] delete must succeed once the ref is cleared")

    _plant_and_assert("zero-padded", "engine:0{id}")
    _plant_and_assert("interior-whitespace", "engine: {id} ")

    # Refutation check: a prefix collision (engine:<id>9) must NOT false-positive.
    eid, ref = _fresh_engine()
    with Session(engine) as s:
        p = Project(name=_uniq("prefix"), source_locale="en-US", default_provider=f"{ref}9")
        s.add(p)
        s.commit()
        s.refresh(p)
        prefix_pid = p.id
    try:
        if engines.reference_count(eid)["total"] != 0:
            _fail(f"prefix false-positive: {ref}9 counted as {ref}")
    finally:
        with Session(engine) as s:
            s.delete(s.get(Project, prefix_pid))
            s.commit()
    engines.delete_profile(eid)

    # FIX 2a: canonicalize_ref() normalizes non-canonical shapes; passes through
    # non-engine / blank values unchanged.
    eid2, ref2 = _fresh_engine()
    try:
        if engines.canonicalize_ref(f"engine:0{eid2}") != ref2:
            _fail("canonicalize_ref did not normalize a zero-padded ref")
        if engines.canonicalize_ref(f" ENGINE:{eid2} ") != ref2:
            _fail("canonicalize_ref did not normalize case/whitespace")
        if engines.canonicalize_ref("claude") != "claude":
            _fail("canonicalize_ref must pass through a non-engine ref unchanged")
        if engines.canonicalize_ref("") != "":
            _fail("canonicalize_ref must pass through a blank value unchanged")
    finally:
        engines.delete_profile(eid2)

    # FIX 2a: the setup-ai save write seam canonicalizes on the way in.
    eid3, ref3 = _fresh_engine()
    try:
        cl = TestClient(app, follow_redirects=False)
        r = cl.post(
            "/setup-ai",
            data={
                "default_provider": f"engine:0{eid3}",
                "model_claude": "",
                "model_gemini": "",
                "model_openai": "",
            },
        )
        if r.status_code not in (302, 303):
            _fail(f"setup-ai save failed: {r.status_code} {r.text}")
        stored = settings.get("default_provider")
        if stored != ref3:
            _fail(
                f"setup-ai save did not canonicalize engine:0{eid3} -> expected "
                f"{ref3!r}, stored {stored!r}"
            )
    finally:
        settings.set_many({"default_provider": "claude"})
        settings.invalidate_cache()
        engines.delete_profile(eid3)

    print(
        "  engine delete fails closed on non-canonical (zero/whitespace-padded) "
        "live refs; no prefix false-positive; setup-ai save canonicalizes on "
        "write (FIX 2): OK"
    )


# ============================================================================
# 9. FIX 3 (LOW): a blank/whitespace segprofile rename is rejected (not a
#    silent no-op "success") at the service + the CLI surface, and writes no
#    old->old audit row.
# ============================================================================
def test_segprofile_blank_rename_rejected():
    prof = segprof.create_profile(_uniq("BlankRen"), actor="test")
    with Session(engine) as s:
        n_before = len(s.exec(select(AuditLog).where(AuditLog.action == "segprofile.rename")).all())

    # service surface
    try:
        segprof.update_profile(prof.id, name="   ")
    except segprof.SegProfileError:
        pass
    else:
        _fail("blank rename must raise SegProfileError at the service, not silently no-op")
    if segprof.get_profile(prof.id).name != prof.name:
        _fail("blank rename must not have changed the stored name")

    # CLI surface: a blank rename must exit non-zero with a clean error, not a
    # silent 'success' + old->old audit row.
    code, out, _err = run(["segprofile", "rename", str(prof.id), "   "])
    if code == 0:
        _fail(f"CLI blank segprofile rename must fail (non-zero exit), got 0: {out}")

    with Session(engine) as s:
        n_after = len(s.exec(select(AuditLog).where(AuditLog.action == "segprofile.rename")).all())
    if n_after != n_before:
        _fail("blank rename wrote a segprofile.rename audit row (old->old) despite being rejected")
    print(
        "  segprofile blank/whitespace rename rejected at service + CLI, no "
        "old->old audit row (FIX 3): OK"
    )


def main():
    test_engine_delete_fails_closed_per_column()
    test_segprofile_service_fails_closed()
    test_segprofile_rename_surfaces()
    test_content_type_rename_audit_detail()
    test_template_rename()
    test_rbac_non_admin_blocked()
    test_engine_delete_global_fallback_referrer()
    test_engine_delete_noncanonical_ref_counted()
    test_segprofile_blank_rename_rejected()
    print("ADMIN DELETE HARDENING OK")


if __name__ == "__main__":
    main()
