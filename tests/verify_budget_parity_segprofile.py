"""verify_budget_parity_segprofile.py -- regression suite for the live-QA fix
wave "D" (T4-02, T4-03, T5-01; see the planning notes).

Hermetic: throwaway LOC_TMS_DATA_DIR, provider keys cleared, ASCII-only prints,
FastAPI TestClient. Every check here FAILS against the pre-fix code (proved via
`git show HEAD:<file>` restoring the original file into the worktree, running
this suite, then restoring the fix) and PASSES after it.

Covers:
  * T4-02 -- the department monthly-token budget ceiling (429 on the /api path
    since Phase B chunk 4) is now ALSO enforced on:
      - the web-UI translate route (POST /documents/{id}/translate): a clean
        429 error page, zero jobs enqueued (documents.py never called
        budgets.check at all pre-fix).
      - the in-process CLI (`doc translate`): a clean nonzero exit with the
        exhausted-budget message, nothing translated (the CLI submitted with
        no budget check at all pre-fix -- it doesn't even go through jobs.py,
        it runs translate.pretranslate_stream() synchronously).
    Both surfaces call the SAME app.services.budgets.check() seam the /api
    path already used -- the CLI's plain --actor email is resolved to a
    department via the new budgets.department_for_actor() helper.
  * T4-03 -- POST /routing/budgets rejects a non-integer monthly_token_limit
    (e.g. "notanumber", the letter-O typo "1O") with a clean redirect error
    instead of silently coercing it to 0 == UNLIMITED (app/web/routing.py's
    bare `except ValueError: limit = 0`). A genuinely valid limit still saves.
  * task_cca6bb89 -- a HUGE integer monthly_token_limit (e.g. 10**30) used to
    reach sqlite3's raw `OverflowError: Python int too large to convert to
    SQLite INTEGER` at the DB bind -- a raw 500 on the web route and an
    unhandled traceback on the CLI verb, because Python's int() has no
    overflow ceiling so the value sailed past both the form parse and
    argparse's type=int unrejected. budgets.upsert_budget now calls the
    shared budgets.validate_limit() bound check BEFORE the DB write, so every
    seam (web /routing/budgets, CLI `admin budget set`) gets a clean error
    instead of a crash. A within-bound huge value (comfortably below the
    MAX_BUDGET_LIMIT ceiling) still saves normally.
  * T5-01 -- segmentation_profiles.delete_profile refuses to delete the
    profile currently flagged is_system_default (a clear SegProfileError,
    fail-closed, mirroring engines.delete_profile's default_fallback_provider
    check) instead of silently succeeding and leaving ZERO default profiles.
    Proved at the service seam AND inherited, with no route changes, by both
    the web /settings/segprofiles/{id}/delete route and the CLI
    `segprofile delete` command (both already catch SegProfileError around
    delete_profile as a pre-existing race guard).
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_budgetseg_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    DepartmentBudget,
    Document,
    Job,
    JobStatus,
    Project,
    Role,
    Segment,
    User,
)
from app.services import budgets, security, settings
from app.services import segmentation_profiles as segprof
from tests._csrf import csrf_for


def run(argv):
    """Invoke the in-process CLI dispatcher and capture (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def _login(cl, who, pw):
    csrf_for(cl)
    r = cl.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(cl)
    return r


def _mkproject(name):
    with Session(engine) as s:
        p = Project(name=name, source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _mkdoc(project_id):
    """A minimal document (no real ingest needed -- every test here is blocked
    BEFORE translation actually runs)."""
    with Session(engine) as s:
        doc = Document(project_id=project_id, filename="t.txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(document_id=doc.id, target_locale="fr-FR", source_text="Hello.", order_index=0)
        )
        s.commit()
        return doc.id


def _seed_spend(department, tokens):
    """One COMPLETED translate_document job stamped `department`, so
    budgets.spend_this_month(department) already exceeds any small cap."""
    with Session(engine) as s:
        j = Job(
            kind="translate_document",
            payload={"document_id": 0, "locale": "fr-FR", "department": department},
            status=JobStatus.COMPLETED,
            result={"tokens_in": tokens, "tokens_out": 0},
            created_by="seed@x.co",
            created_at=datetime.utcnow(),
        )
        s.add(j)
        s.commit()


def _job_count():
    with Session(engine) as s:
        return len(s.exec(select(Job.id).where(Job.kind == "translate_document")).all())


# --- T4-02: web translate honors the department budget ceiling --------------
def test_web_translate_budget_blocked():
    """A department that has exhausted its monthly cap gets a clean 429 (not a
    500, not a silent enqueue) from the WEB translate route."""
    dept = "webbudget"
    admin = security.create_user("webbudget-admin@x.co", "WB Admin", Role.ADMIN, "wb-pass-123456")
    with Session(engine) as s:
        row = s.get(User, admin.id)
        row.department = dept
        s.add(row)
        s.commit()
    with Session(engine) as s:
        s.add(DepartmentBudget(department=dept, monthly_token_limit=10))
        s.commit()
    _seed_spend(dept, 999)  # already blown past the 10-token cap

    pid = _mkproject("Web budget proj")
    doc_id = _mkdoc(pid)

    settings.set_many({"security_auth_required": "1"})
    try:
        c = TestClient(app, follow_redirects=False)
        assert _login(c, "webbudget-admin@x.co", "wb-pass-123456").status_code == 303
        before = _job_count()
        r = c.post(f"/documents/{doc_id}/translate", data={"locale": "fr-FR"})
        assert r.status_code == 429, (r.status_code, r.text)
        # Jinja2 autoescapes the apostrophe in EXHAUSTED_MESSAGE (-> &#39;) in the
        # rendered error page, so match on a substring that survives escaping.
        assert "translation budget for this month is used up" in r.text, r.text
        after = _job_count()
        assert after == before, "over-budget web translate must enqueue ZERO jobs"
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  web translate: over-budget department -> clean 429, zero jobs enqueued")


# --- T4-02: CLI translate honors the department budget ceiling ---------------
def test_cli_translate_budget_blocked():
    """The in-process `doc translate` command refuses to run for an over-budget
    department: a clean nonzero exit (matching /api's 429), nothing translated."""
    dept = "clibudget"
    email = "clibudget@x.co"
    u = security.create_user(email, "CLI Budget", Role.TRANSLATOR, "cb-pass-123456")
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.department = dept
        s.add(row)
        s.commit()
    with Session(engine) as s:
        s.add(DepartmentBudget(department=dept, monthly_token_limit=10))
        s.commit()
    _seed_spend(dept, 999)

    pid = _mkproject("CLI budget proj")
    doc_id = _mkdoc(pid)

    code, _out, err = run(["--actor", email, "doc", "translate", str(doc_id), "--locale", "fr-FR"])
    assert code != 0, f"over-budget CLI translate must exit nonzero, got {code}"
    assert budgets.EXHAUSTED_MESSAGE in err, err
    with Session(engine) as s:
        seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).first()
        assert not (seg.target_text or "").strip(), (
            "over-budget CLI translate must not have translated anything"
        )
    print("  cli translate: over-budget department -> clean nonzero exit, nothing translated")


# --- T4-02 sanity: a department with NO budget is unaffected -----------------
def test_cli_translate_no_budget_allowed_past_the_gate():
    """A department with no DepartmentBudget row is unlimited -- the new gate
    must not block callers it was never meant to touch. (We only assert the
    budget gate itself passes -- i.e. the command does NOT fail with the
    exhausted-budget message; MockProvider/no-key posture may still make the
    actual translate a no-op, which is fine and unrelated to this fix.)"""
    email = "cli-nobudget@x.co"
    security.create_user(email, "No Budget", Role.TRANSLATOR, "nb-pass-123456")
    pid = _mkproject("CLI no-budget proj")
    doc_id = _mkdoc(pid)
    _code, _out, err = run(["--actor", email, "doc", "translate", str(doc_id), "--locale", "fr-FR"])
    assert budgets.EXHAUSTED_MESSAGE not in err, (
        "a no-budget department must never be blocked by the budget gate"
    )
    print("  cli translate: no-budget department is not blocked by the new gate")


# --- T4-03: a garbage budget limit is rejected, not coerced to unlimited -----
def test_web_budget_garbage_limit_rejected():
    dept = "garbagedept"
    settings.set_many({"security_auth_required": "1"})
    try:
        c = TestClient(app, follow_redirects=False)
        assert _login(c, "admin", "admin").status_code == 303
        r = c.post(
            "/routing/budgets", data={"department": dept, "monthly_token_limit": "notanumber"}
        )
        assert r.status_code == 303 and "error=" in r.headers["location"], r.headers
        with Session(engine) as s:
            row = s.exec(
                select(DepartmentBudget).where(DepartmentBudget.department == dept)
            ).first()
        assert row is None, (
            "a garbage limit must be REJECTED, not silently create an unlimited (0) row",
            row,
        )

        # A typo that still LOOKS numeric-ish (letter "O", not zero) is
        # equally rejected -- not coerced to 0.
        r = c.post("/routing/budgets", data={"department": dept, "monthly_token_limit": "1O"})
        assert r.status_code == 303 and "error=" in r.headers["location"], r.headers
        with Session(engine) as s:
            row = s.exec(
                select(DepartmentBudget).where(DepartmentBudget.department == dept)
            ).first()
        assert row is None, row

        # Sanity: the fix doesn't over-reject -- a genuinely valid limit saves.
        r = c.post("/routing/budgets", data={"department": dept, "monthly_token_limit": "500"})
        assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
        with Session(engine) as s:
            row = s.exec(
                select(DepartmentBudget).where(DepartmentBudget.department == dept)
            ).first()
        assert row is not None and row.monthly_token_limit == 500, row

        # Sanity: an explicit "0" is still accepted and still means unlimited
        # (0 is a legitimate sentinel, only GARBAGE is rejected).
        r = c.post("/routing/budgets", data={"department": dept, "monthly_token_limit": "0"})
        assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
        with Session(engine) as s:
            row = s.exec(
                select(DepartmentBudget).where(DepartmentBudget.department == dept)
            ).first()
        assert row is not None and row.monthly_token_limit == 0, row
    finally:
        settings.set_many({"security_auth_required": "0"})
    print(
        "  budgets: non-integer limit rejected (no coercion to 0); explicit 0 "
        "and a valid limit still save"
    )


# --- task_cca6bb89: a HUGE budget value is a clean error, never a raw 500 ----
def test_web_budget_huge_limit_rejected():
    """A monthly_token_limit far outside SQLite's int64 range gets a clean
    redirect error (never the raw sqlite3 OverflowError -> 500). Both an
    absurdly large POSITIVE and NEGATIVE value are rejected; a within-bound
    huge value still saves (the fix doesn't over-reject)."""
    dept = "hugedept"
    huge = 10**30
    settings.set_many({"security_auth_required": "1"})
    try:
        c = TestClient(app, follow_redirects=False)
        assert _login(c, "admin", "admin").status_code == 303

        r = c.post("/routing/budgets", data={"department": dept, "monthly_token_limit": str(huge)})
        assert r.status_code == 303, (r.status_code, r.text[:300])
        assert "error=" in r.headers["location"], r.headers
        with Session(engine) as s:
            row = s.exec(
                select(DepartmentBudget).where(DepartmentBudget.department == dept)
            ).first()
        assert row is None, (
            "a huge limit must be REJECTED, not crash into a half-written row",
            row,
        )

        # A huge NEGATIVE magnitude is equally rejected (it would ALSO overflow
        # the SQLite bind, even though <=0 is semantically "unlimited").
        r = c.post("/routing/budgets", data={"department": dept, "monthly_token_limit": str(-huge)})
        assert r.status_code == 303 and "error=" in r.headers["location"], r.headers
        with Session(engine) as s:
            row = s.exec(
                select(DepartmentBudget).where(DepartmentBudget.department == dept)
            ).first()
        assert row is None, row

        # Sanity: a within-bound huge value still saves normally (no over-reject).
        big_ok = 10**12
        r = c.post(
            "/routing/budgets", data={"department": dept, "monthly_token_limit": str(big_ok)}
        )
        assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
        with Session(engine) as s:
            row = s.exec(
                select(DepartmentBudget).where(DepartmentBudget.department == dept)
            ).first()
        assert row is not None and row.monthly_token_limit == big_ok, row
    finally:
        settings.set_many({"security_auth_required": "0"})
    print(
        "  budgets: huge (+/-) limit -> clean redirect error, never a raw 500; "
        "a within-bound huge value still saves"
    )


def test_cli_budget_huge_limit_rejected():
    """`admin budget set <dept> <huge>` gets a clean nonzero-exit CliError
    (never an unhandled OverflowError traceback)."""
    dept = "clihugedept"
    huge = 10**30
    code, out, err = run(["admin", "budget", "set", dept, str(huge)])
    assert code != 0, f"a huge --limit must exit nonzero, got {code}: out={out!r} err={err!r}"
    assert "OverflowError" not in err, f"must be a clean CliError, not a raw traceback: {err}"
    assert "too large" in err.lower(), err
    with Session(engine) as s:
        row = s.exec(select(DepartmentBudget).where(DepartmentBudget.department == dept)).first()
    assert row is None, row
    print("  cli budget set: huge limit -> clean nonzero exit + CliError, not a crash")


# --- T5-01: deleting the system-default segprofile is refused ---------------
def test_segprofile_delete_system_default_refused():
    default_prof = segprof.create_profile("Guard default", is_system_default=True)
    other_prof = segprof.create_profile("Guard non-default")
    try:
        try:
            segprof.delete_profile(default_prof.id)
            raise AssertionError("deleting the system-default profile must be refused")
        except segprof.SegProfileError as exc:
            assert "system default" in str(exc).lower(), exc
        assert segprof.get_profile(default_prof.id) is not None, (
            "the system-default profile must survive the refused delete"
        )
        assert segprof.get_profile(default_prof.id).is_system_default is True

        # A NON-default profile still deletes normally (the guard is targeted,
        # not a blanket regression on delete_profile).
        assert segprof.delete_profile(other_prof.id) is True
        assert segprof.get_profile(other_prof.id) is None
    finally:
        segprof.update_profile(default_prof.id, is_system_default=False)
        segprof.delete_profile(default_prof.id)
    print(
        "  segprofile delete: system-default refused with a clear message; "
        "non-default still deletes"
    )


def test_segprofile_delete_system_default_refused_web_and_cli():
    """The refusal is inherited by BOTH the web /settings route and the CLI
    `segprofile delete` command through the shared delete_profile seam -- no
    route/CLI code changes needed, they already catch SegProfileError as a
    pre-existing check-then-delete race guard."""
    prof = segprof.create_profile("Guard web/cli default", is_system_default=True)
    settings.set_many({"security_auth_required": "1"})
    try:
        c = TestClient(app, follow_redirects=False)
        assert _login(c, "admin", "admin").status_code == 303
        r = c.post(f"/settings/segprofiles/{prof.id}/delete")
        assert "srx_error=" in r.headers["location"], r.headers
        assert segprof.get_profile(prof.id) is not None
    finally:
        settings.set_many({"security_auth_required": "0"})
    code, _out, _err = run(["segprofile", "delete", str(prof.id), "--yes"])
    assert code != 0, "CLI delete of the system-default profile must fail"
    assert segprof.get_profile(prof.id) is not None
    segprof.update_profile(prof.id, is_system_default=False)
    segprof.delete_profile(prof.id)
    print("  segprofile delete: refusal inherited by BOTH the web and CLI routes")


def main():
    init_db()
    security.seed_initial()  # admin/admin + linguist + user1
    settings.set_many({"audit_enabled": "1", "security_auth_required": "0"})
    test_web_translate_budget_blocked()
    test_cli_translate_budget_blocked()
    test_cli_translate_no_budget_allowed_past_the_gate()
    test_web_budget_garbage_limit_rejected()
    test_web_budget_huge_limit_rejected()
    test_cli_budget_huge_limit_rejected()
    test_segprofile_delete_system_default_refused()
    test_segprofile_delete_system_default_refused_web_and_cli()
    print("BUDGET PARITY + SEGPROFILE GUARD OK")


if __name__ == "__main__":
    main()
