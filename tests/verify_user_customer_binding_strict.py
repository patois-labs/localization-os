"""#485: POST /api/users/{id}/customer needs a customer_id or clear:true.

THE BUG: an empty body (or {"customer_id": null}) reached security.set_customer with
None, which means "clear", so a docs-scoped admin key silently turned a customer-bound
account into internal staff that sees every customer's data. The documented contract is
customer_id OR clear:true; anything else is a 400 and changes nothing.

Final line: USER CUSTOMER BINDING STRICT OK.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_custbind_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Customer, Role, User
from app.services import apikeys, security, settings

init_db()
settings.set_many(
    {"security_auth_required": "1", "multi_org_enabled": "0", "remote_admin_enabled": "1"}
)
settings.invalidate_cache()

_PW = "custbind-fixture-pass-1"
admin = security.create_user("cb-admin@x.com", "Admin", Role.ADMIN, _PW)
with Session(engine) as s:
    cust = Customer(name="Acme CB")
    s.add(cust)
    s.commit()
    s.refresh(cust)
    cid = cust.id
bound = security.create_user("cb-bound@x.com", "Bound", Role.TRANSLATOR, _PW, customer_id=cid)
_row, raw = apikeys.create_key(admin.id, "cb", scopes="api docs")
H = {"X-Loc-Token": raw}
c = TestClient(app)


def _cust():
    with Session(engine) as s:
        return s.get(User, bound.id).customer_id


def main():
    assert _cust() == cid
    for body in (
        {},
        {"customer_id": None},
        {"customer_id": ""},
        {"clear": "yes"},
        {"clear": False},
        {"clear": 1},
    ):
        r = c.post(f"/api/users/{bound.id}/customer", headers=H, json=body)
        assert r.status_code == 400, (body, r.status_code, r.text[:200])
        assert r.json().get("ok") is False, (body, r.text)
        assert _cust() == cid, ("binding must be untouched", body)
    # the documented forms still work
    r = c.post(f"/api/users/{bound.id}/customer", headers=H, json={"clear": True})
    assert r.status_code == 200 and _cust() is None, r.text
    r = c.post(f"/api/users/{bound.id}/customer", headers=H, json={"customer_id": cid})
    assert r.status_code == 200 and _cust() == cid, r.text
    print("  empty / null / non-boolean bodies are 400 and leave the binding alone")
    check_cli_blank_customer_refused()
    print("USER CUSTOMER BINDING STRICT OK")


def check_cli_blank_customer_refused():
    """#485 gate (CLI parity): `user customer <u> --customer ""` used to reach set_customer
    with "" (= unbind) and clear the binding; only --clear-customer may clear."""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.__main__ import main as cli_main

    def _cli(argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                return cli_main(argv)
            except SystemExit as exc:
                return exc.code if isinstance(exc.code, int) else 2

    assert _cust() == cid
    for blank in ("", "  ", "0"):
        code = _cli(["user", "customer", "cb-bound@x.com", "--customer", blank])
        assert code != 0, ("blank --customer must fail", repr(blank))
        assert _cust() == cid, ("binding must be untouched", repr(blank))
    assert _cli(["user", "customer", "cb-bound@x.com", "--clear-customer"]) == 0
    assert _cust() is None
    assert _cli(["user", "customer", "cb-bound@x.com", "--customer", str(cid)]) == 0
    assert _cust() == cid
    print("  CLI: a blank --customer is refused; --clear-customer still clears")


if __name__ == "__main__":
    main()
