"""Wave-0 audit bug regressions for the `admin` CLI surface (offline, hermetic).

Covers three fixes, all on app/cli/cmd_admin.py:

  BUG-1 (multi-org scoping gap): `admin gdpr export/erase`, `admin audit export`,
     and `admin budget set/list/delete` never threaded the operator's resolved org
     scope (args._op_org_id via --org), unlike their correctly-scoped `user
     export/erase` twins + the web forms. With the sacred gate ON, an org-A-scoped
     operator could export/erase an org-B user, list/see/delete org-B budgets, and
     export org-B audit rows. Each verb below is now scoped; the gate OFF stays
     byte-identical (no --org needed, no scoping bites).

  BUG-2 (argparse dest collision): `admin audit export --actor` used to clobber the
     GLOBAL `--actor` (dest="actor"), zeroing the audit-attribution actor. The
     subcommand flag is now dest="filter_actor"; the global --actor survives.

  BUG-5 (audit verify convergence): `audit verify` (top-level) and `admin audit
     verify` ran two divergent implementations. Both now delegate to the ONE shared
     core (cmd_db.verify_audit_chain), so a broken chain yields the SAME exit code
     (1) + structured payload on both.

ASCII-only output; standalone one-process-per-suite script (LOC_TMS_DATA_DIR is a
throwaway temp dir, provider keys cleared -> MockProvider, deterministic + free).

Run standalone: python tests/verify_admin_org_scope.py  ->  prints "... OK".
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

init_env("loctms_admin_scope_")

from sqlalchemy import text
from sqlmodel import Session, select

from app.cli.__main__ import build_parser
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    AuditLog,
    DepartmentBudget,
    Organization,
    Role,
    User,
)
from app.services import audit, settings


# --------------------------------------------------------------------------- #
# CLI drivers (in-process, capturing exit code + stdout + stderr).
def _cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def _cli_json(argv):
    code, out, err = _cli(["--json"] + argv)  # noqa: RUF005
    payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    return code, payload, err


def _gate_on():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()


def _gate_off():
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _seed_two_orgs():
    """Two orgs A and B, each with one user. Returns (org_a_id, org_b_id, users)."""
    init_db()
    with Session(engine) as s:
        org_a = Organization(name="Scope Org A")
        org_b = Organization(name="Scope Org B")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)

        # #74: REUSE the fixture accounts across calls, re-stamping them into the NEW
        # orgs. They used to be re-INSERTed every call, quietly piling up duplicate
        # ascope@a.test / bscope@b.test rows -- the identity ambiguity #74 closes, and a
        # by-email `.first()` then resolved to the OLDEST row (a STALE org). The DB-level
        # ux_user_email_lower now refuses the second INSERT outright.
        def _fixture_user(email, name, org_id):
            u = s.exec(select(User).where(User.email == email)).first()
            if u is None:
                u = User(email=email, name=name, role=Role.TRANSLATOR, org_id=org_id)
            else:
                u.org_id = org_id
                u.active = True
            s.add(u)
            return u

        ua = _fixture_user("ascope@a.test", "A User", org_a.id)
        ub = _fixture_user("bscope@b.test", "B User", org_b.id)
        s.commit()
        s.refresh(ua)
        s.refresh(ub)
        return (
            org_a.id,
            org_b.id,
            {"a_uid": ua.id, "a_email": ua.email, "b_uid": ub.id, "b_email": ub.email},
        )


# --------------------------------------------------------------------------- #
# BUG-2: the global --actor is not clobbered by `admin audit export --actor`.
def test_bug2_actor_dest_no_collision():
    p = build_parser()
    a = p.parse_args(
        ["--actor", "boss@corp.test", "admin", "audit", "export", "--actor", "someone@else.test"]
    )
    assert a.actor == "boss@corp.test", ("global --actor was clobbered", a.actor)
    assert a.filter_actor == "someone@else.test", ("subcommand filter lost", a)
    # A bare `admin audit export` leaves the global --actor at its default.
    b = p.parse_args(["--actor", "boss@corp.test", "admin", "audit", "export"])
    assert b.actor == "boss@corp.test" and b.filter_actor == "", b
    print("  BUG-2: admin audit export --actor -> filter_actor; global --actor intact")


# --------------------------------------------------------------------------- #
# BUG-1: gdpr export/erase are org-scoped.
def test_bug1_gdpr_export_erase_scoped():
    org_a, _org_b, u = _seed_two_orgs()
    settings.set_many({"audit_enabled": "1"})
    _gate_on()
    try:
        # Org-A operator exporting org-A's own user succeeds.
        code, pay, err = _cli_json(
            ["--org", str(org_a), "admin", "gdpr", "export", str(u["a_uid"])]
        )
        assert code == 0 and pay["ok"], (code, pay, err)
        # Org-A operator exporting org-B's user is REFUSED (no such user).
        code, _pay, err = _cli(["--org", str(org_a), "admin", "gdpr", "export", str(u["b_uid"])])
        assert code == 2 and "no such user" in err.lower(), (code, err)
        # Org-A operator erasing org-B's user is REFUSED; the user survives.
        code, _pay, err = _cli(
            ["--org", str(org_a), "admin", "gdpr", "erase", str(u["b_uid"]), "--yes"]
        )
        assert code != 0, ("cross-org erase must fail", code, err)
        with Session(engine) as s:
            b = s.get(User, u["b_uid"])
            assert b is not None and b.email == u["b_email"], (
                "org-B user was erased across orgs",
                b,
            )
        print(
            "  BUG-1 gdpr: cross-org export refused (no such user); cross-org erase "
            "refused, org-B user intact"
        )
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# Reviewer 1 F1: erase must not leak a cross-org EXISTENCE ORACLE. Pre-fix, both
# erase paths resolved the foreign user, printed their EMAIL in the confirm()
# prompt, and failed with a DIFFERENT (exit 1, "User not found.") shape than a
# truly-missing uid (exit 2, "no such user: N") -- so a scoped operator could
# both confirm a foreign uid exists and harvest its email. Post-fix, cross-org
# and missing are INDISTINGUISHABLE (same exit 2, same message), and the email
# never reaches any output stream (the pre-check runs BEFORE confirm()).
def test_r1f1_erase_no_existence_oracle():
    org_a, _org_b, u = _seed_two_orgs()
    settings.set_many({"audit_enabled": "1"})
    _gate_on()
    try:
        missing_uid = 987654  # no such row anywhere
        for verb in (["admin", "gdpr", "erase"], ["user", "erase"]):
            # Baseline: a truly-missing uid (no --yes: the oracle window is the
            # confirm() prompt, so drive the interactive path).
            code_m, _out_m, err_m = _cli(["--org", str(org_a)] + verb + [str(missing_uid)])  # noqa: RUF005
            assert code_m == 2 and f"no such user: {missing_uid}" in err_m, (verb, code_m, err_m)
            # Cross-org uid: byte-uniform with missing (modulo the uid itself).
            code_x, out_x, err_x = _cli(["--org", str(org_a)] + verb + [str(u["b_uid"])])  # noqa: RUF005
            assert code_x == 2, (verb, "cross-org erase must exit 2 like missing", code_x, err_x)
            assert f"no such user: {u['b_uid']}" in err_x, (verb, err_x)
            assert err_m.replace(str(missing_uid), "N") == err_x.replace(str(u["b_uid"]), "N"), (
                verb,
                "cross-org vs missing stderr must be indistinguishable",
                err_m,
                err_x,
            )
            # The foreign email is never disclosed on ANY stream (pre-fix the
            # confirm() prompt printed it before the refusal).
            for stream in (out_x, err_x):
                assert u["b_email"] not in stream, (verb, "foreign email leaked", stream)
            # And the foreign user is untouched.
            with Session(engine) as s:
                b = s.get(User, u["b_uid"])
                assert b is not None and b.email == u["b_email"], b
        print(
            "  R1-F1: cross-org erase == missing-uid (exit 2, identical message); "
            "no email disclosed pre-confirm; both erase twins uniform"
        )
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# BUG-1: audit export is org-scoped (only home-org actors' rows ship).
def test_bug1_audit_export_scoped():
    org_a, _org_b, u = _seed_two_orgs()
    settings.set_many({"audit_enabled": "1"})
    # Two audit rows, one authored by each org's user (audit rows belong to an org
    # via the org_id stamped from their actor at write time -- see audit._actor_org_id, #487).
    audit.log(actor=u["a_email"], action="scope.test.a", target="ta")
    audit.log(actor=u["b_email"], action="scope.test.b", target="tb")
    _gate_on()
    try:
        code, pay, err = _cli_json(["--org", str(org_a), "admin", "audit", "export"])
        assert code == 0, (code, err)
        actors = {r.get("actor") for r in pay.get("rows", [])}
        assert u["a_email"] in actors, ("org-A actor row missing", actors)
        assert u["b_email"] not in actors, ("org-B actor row leaked across orgs", actors)
        print("  BUG-1 audit: --org confines the export to home-org actors (org-B rows excluded)")
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# BUG-1: budget set/list/delete are org-scoped.
def test_bug1_budget_scoped():
    org_a, org_b, _u = _seed_two_orgs()
    _gate_on()
    try:
        # Each org creates a budget for the SAME department name -> two distinct rows.
        code, pay_a, err = _cli_json(
            ["--org", str(org_a), "admin", "budget", "set", "engineering", "1000"]
        )
        assert code == 0 and pay_a["ok"], (code, pay_a, err)
        code, pay_b, err = _cli_json(
            ["--org", str(org_b), "admin", "budget", "set", "engineering", "2000"]
        )
        assert code == 0 and pay_b["ok"], (code, pay_b, err)
        assert pay_a["id"] != pay_b["id"], ("shared dept name must keep per-org rows", pay_a, pay_b)
        with Session(engine) as s:
            assert s.get(DepartmentBudget, pay_a["id"]).org_id == org_a
            assert s.get(DepartmentBudget, pay_b["id"]).org_id == org_b

        # list is confined to the scoping org.
        code, lst_a, err = _cli_json(["--org", str(org_a), "admin", "budget", "list"])
        assert code == 0, (code, err)
        ids_a = {b["id"] for b in lst_a["budgets"]}
        assert pay_a["id"] in ids_a and pay_b["id"] not in ids_a, (
            "org-A list leaked org-B budget",
            ids_a,
        )

        # delete of a cross-org budget id is REFUSED (no such budget); the row survives.
        code, _pay, err = _cli(
            ["--org", str(org_a), "admin", "budget", "delete", str(pay_b["id"]), "--yes"]
        )
        assert code == 2 and "no such budget" in err.lower(), (code, err)
        with Session(engine) as s:
            assert s.get(DepartmentBudget, pay_b["id"]) is not None, (
                "org-B budget was deleted across orgs"
            )
        # delete of the operator's OWN-org budget works.
        code, pay, err = _cli_json(
            ["--org", str(org_a), "admin", "budget", "delete", str(pay_a["id"]), "--yes"]
        )
        assert code == 0 and pay.get("deleted"), (code, pay, err)
        print(
            "  BUG-1 budget: per-org rows for a shared dept; list/delete confined "
            "to the scoping org (cross-org delete refused)"
        )
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# BUG-1: the sacred gate OFF -> no --org needed, scoping is inert (byte-identical).
def test_bug1_gate_off_byte_identical():
    init_db()
    _gate_off()
    settings.set_many({"audit_enabled": "1"})
    with Session(engine) as s:
        u = User(email="single@tenant.test", name="Solo", role=Role.TRANSLATOR)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    # No --org, no --operator: every verb runs clean (gate off = single-tenant).
    code, pay, err = _cli_json(["admin", "gdpr", "export", str(uid)])
    assert code == 0 and pay["ok"], (code, pay, err)
    code, pay, err = _cli_json(["admin", "audit", "export"])
    assert code == 0, (code, err)
    code, pay, err = _cli_json(["admin", "budget", "set", "solo-dept", "500"])
    assert code == 0 and pay["ok"], (code, pay, err)
    bid = pay["id"]
    code, pay, err = _cli_json(["admin", "budget", "list"])
    assert code == 0 and any(b["id"] == bid for b in pay["budgets"]), (code, pay)
    code, pay, err = _cli_json(["admin", "budget", "delete", str(bid), "--yes"])
    assert code == 0 and pay.get("deleted"), (code, pay, err)
    print(
        "  BUG-1 gate-off: admin gdpr/audit/budget run with no --org (byte-identical single-tenant)"
    )


# --------------------------------------------------------------------------- #
# BUG-5: `audit verify` and `admin audit verify` converge (both delegate to the
# shared core -> same exit + payload on an intact chain).
def test_bug5_audit_verify_converges():
    init_db()
    _gate_off()
    settings.set_many({"audit_enabled": "1"})
    # A short clean chain first: both registrations agree it is intact.
    audit.log(actor="cli", action="converge.a", target="x")
    audit.log(actor="cli", action="converge.b", target="y")
    audit.log(actor="cli", action="converge.c", target="z")
    code1, pay1, _ = _cli_json(["audit", "verify"])
    code2, pay2, _ = _cli_json(["admin", "audit", "verify"])
    assert code1 == 0 and pay1["ok"] is True, pay1
    assert code2 == 0 and pay2["ok"] is True, pay2
    # Both registrations return the SAME payload (the point of BUG-5); the verify payload
    # has since gained fields (audit_enabled, note), so require the core keys, not an
    # exact key set.
    assert pay1 == pay2 and {"ok", "checked", "first_bad_id"} <= set(pay1), (pay1, pay2)

    # Now TAMPER the chain and prove the ERROR HANDLING converges: pre-fix the two
    # registrations diverged here -- `audit verify` raised CliError (exit 1 + a loud
    # stderr message), while `admin audit verify` merely `return 1`ed with NO CliError
    # message. Both must now behave identically.
    with Session(engine) as s:
        victim_id = s.exec(select(AuditLog).order_by(AuditLog.id)).all()[1].id
        s.exec(
            text("UPDATE auditlog SET detail = 'tampered' WHERE id = :i").bindparams(i=victim_id)
        )
        s.commit()
    try:
        c1, out1, err1 = _cli(["audit", "verify"])
        c2, out2, err2 = _cli(["admin", "audit", "verify"])
        assert c1 == c2 == 1, ("both must exit 1 on a broken chain", c1, c2)
        assert "verification failed" in err1.lower(), ("top-level lost its error", err1)
        assert "verification failed" in err2.lower(), (
            "admin audit verify did not converge onto the shared CliError",
            err2,
        )
        assert "broken" in out1.lower() and "broken" in out2.lower(), (out1, out2)
    finally:
        with Session(engine) as s:
            s.exec(text("UPDATE auditlog SET detail = '' WHERE id = :i").bindparams(i=victim_id))
            s.commit()
    print(
        "  BUG-5: audit verify + admin audit verify share one core "
        "(same exit + BROKEN message + CliError on a tampered chain)"
    )


def main():
    test_bug2_actor_dest_no_collision()
    test_bug1_gdpr_export_erase_scoped()
    test_r1f1_erase_no_existence_oracle()
    test_bug1_audit_export_scoped()
    test_bug1_budget_scoped()
    test_bug1_gate_off_byte_identical()
    test_bug5_audit_verify_converges()
    print("ADMIN ORG SCOPE OK")


if __name__ == "__main__":
    main()
