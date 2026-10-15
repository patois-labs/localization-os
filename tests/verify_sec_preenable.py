"""Pre-enablement security regressions, using only a throwaway two-org install."""

import asyncio
import inspect
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from tests._bootstrap import init_env

init_env("loctms_sec_preenable_")
sys.path.insert(0, str(ROOT / "integrations" / "mcp-server"))
import server as mcp_server
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import Customer, Organization, Role, User
from app.services import apikeys, ldap_auth, security, settings
from tests._csrf import csrf_for

PASSWORD = "Throwaway-Preenable-42!"  # pregate: allow-secret test-only credential
LOGIN_FIELD = "password"  # pregate: allow-secret form field name
OPERATIONS = {
    "role": {"role": Role.VIEWER},
    "customer": {},
    "department": {"department": "changed"},
    "apikeys/revoke": {},
    "toggle": {},
}


def snapshot(uid):
    with Session(engine) as s:
        u = s.get(User, uid)
        return (u.model_dump(), [(k.id, k.active) for k in apikeys.list_keys(uid)])


def setup():
    init_db()
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    with Session(engine) as s:
        a, b = Organization(name="Home Org"), Organization(name="Foreign Org Hidden")
        s.add(a)
        s.add(b)
        s.commit()
        aid, bid = a.id, b.id
        cust = Customer(name="Home customer", org_id=aid)
        s.add(cust)
        s.commit()
        cid = cust.id
    admin = security.create_user("admin@home.test", "Home Admin", Role.ADMIN, PASSWORD, org_id=aid)
    security.create_user("admin@foreign.test", "Foreign Admin", Role.ADMIN, "", org_id=bid)
    with Session(engine) as s:
        for i in range(55):
            s.add(User(email=f"home{i}@home.test", name=f"Match Home {i}", org_id=aid))
        for i in range(4):
            s.add(User(email=f"foreign{i}@foreign.test", name=f"Match Foreign {i}", org_id=bid))
        s.commit()
    client = TestClient(app)
    csrf_for(client)
    r = client.post(
        "/auth/login",
        data={"email": admin.email, LOGIN_FIELD: PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    csrf_for(client)
    return client, admin.id, aid, bid, cid


def operation(client, org, cid, name, expected):
    u = security.create_user(
        f"{name.replace('/', '-')}@{org}.test", "Target", Role.TRANSLATOR, "", org_id=org
    )
    apikeys.create_key(u.id, "throwaway")
    before = snapshot(u.id)
    payload = {"customer_id": str(cid)} if name == "customer" else OPERATIONS[name]
    r = client.post(f"/users/{u.id}/{name}", data=payload, follow_redirects=False)
    after = snapshot(u.id)
    if expected == 404:
        assert after == before, f"cross-org {name} changed the target"
        assert r.status_code == 404, (name, r.status_code)
    else:
        assert r.status_code == 303, (name, r.status_code)
        assert after != before, f"same-org {name} did not operate"


def last_admin(client, uid, name):
    before = snapshot(uid)
    r = client.post(f"/users/{uid}/{name}", data=OPERATIONS[name], follow_redirects=False)
    after = snapshot(uid)
    # Restore even on red so both guards are tested independently.
    with Session(engine) as s:
        u = s.get(User, uid)
        u.role, u.active = Role.ADMIN, True
        s.add(u)
        s.commit()
    assert after == before, f"last admin {name} changed the target"
    assert r.status_code == 409


def listing(client, aid, query):
    r = client.get("/users" + query)
    assert r.status_code == 200
    ctx = r.context
    assert all(u.org_id == aid for u in ctx["users"]), "foreign user in page"
    assert ctx["total"] == (55 if "q=Match" in query else 56), ctx["total"]
    assert "Foreign Org Hidden" not in ctx["org_names"].values()
    assert "Foreign Org Hidden" not in r.text
    assert ctx["users"], "own users missing"


def mcp_read(http, planner):
    src = Path(config.DATA_DIR) / "mcp-input.txt"
    src.write_bytes(b"first\nsecond\n")
    with (
        patch.object(mcp_server, "_LOCAL_FILE_READS", not http, create=True),
        # #755: a stdio read is confined to the allowed input folders; this file's folder
        patch.object(mcp_server, "_INPUT_ROOTS", [str(config.DATA_DIR)], create=True),
        patch.object(mcp_server, "TOKEN", ""),  # pregate: allow-secret no auth
        patch.object(mcp_server, "_call", side_effect=AssertionError("outbound call")),
    ):
        try:
            if planner == "dispatch":
                result = asyncio.run(
                    mcp_server.call_tool_unvalidated(
                        "submit_translation", {"file_path": str(src), "targets": []}
                    )
                )
            elif planner:
                result = asyncio.run(
                    mcp_server._plan_submit_translation({"file_path": str(src), "targets": []})
                )
            else:
                result = mcp_server._read_local_file(str(src))
        except mcp_server.ToolError as exc:
            assert http and "file_path" in str(exc) and "data" in str(exc)
        else:
            assert not http, f"HTTP host read accepted: {planner}: {result}"
            if not planner:
                assert result == b"first\nsecond\n"
            else:
                assert "source_bytes" in str(result) and "source_lines" in str(result)


def service_scopes(aid, bid, cid):
    u = security.create_user(
        "service@foreign.test", "Service Target", Role.TRANSLATOR, "", org_id=bid
    )
    apikeys.create_key(u.id, "throwaway")
    before = snapshot(u.id)
    with Session(engine) as session:
        calls = [
            (security.set_role, (session, u.id, Role.VIEWER)),
            (security.set_active, (session, u.id, False)),
            (security.set_department, (session, u.id, "changed")),
            (security.set_customer, (session, u.id, cid)),
            (apikeys.revoke_all_for_user, (u.id,)),
        ]
        for fn, args in calls:
            assert inspect.signature(fn).parameters["org_id"].default is inspect.Parameter.empty
            for scope in (aid, None, -1):
                result = fn(*args, org_id=scope)
                assert result == 0 or not result["ok"], fn.__name__
                assert snapshot(u.id) == before, fn.__name__


def startup_transport():
    """The permission must be settled BEFORE the listener starts, and it must fail closed
    for any start path that is not local stdio.

    The first version of this case asserted the flag only AFTER run() returned, so it
    passed against a main() that set the flag too late -- the reviewer proved that by
    running it against a fake main() which assigned after run(). Asserting inside the run
    mock is the whole point: by the time a listener is accepting calls, the answer has to
    be final."""
    old_http, old_reads = mcp_server._HTTP_MODE, mcp_server._LOCAL_FILE_READS
    try:
        for transport, expect_reads in (("streamable-http", False), ("stdio", True)):
            seen = {}

            def capture(*_a, _seen=seen, **_kw):
                # Sampled INSIDE run(), i.e. at the moment the listener would start.
                _seen["reads"] = mcp_server._LOCAL_FILE_READS

            with patch.object(mcp_server.mcp, "run", side_effect=capture) as run:
                mcp_server.main(["--transport", transport])
                run.assert_called_once()
            assert seen.get("reads") is expect_reads, (
                f"{transport}: local file reads were {seen.get('reads')!r} when the "
                f"listener started, expected {expect_reads!r}"
            )

        # Fail closed: a start path that never calls main() (importing the module and
        # serving mcp.streamable_http_app() directly, which is how verify_mcp_connector
        # stands up its listener) must leave reads DENIED rather than enabled.
        mcp_server._allow_local_file_reads(False)
        for fn in (
            lambda: mcp_server._read_local_file(__file__),
            lambda: asyncio.run(mcp_server._plan_submit_translation({"file_path": __file__})),
        ):
            try:
                fn()
            except Exception as exc:
                assert "local stdio" in str(exc), str(exc)
            else:
                raise AssertionError("an uninitialised server still read a caller path")
    finally:
        mcp_server._HTTP_MODE = old_http
        mcp_server._allow_local_file_reads(old_reads)


def listing_has_no_cross_org_surface(client, aid):
    """Was `superadmin_listing` (GATE R1 MED-1): a super-admin's CUSTOMER-APP session used
    to be handed every org's name, because `can_move_org` short-circuited the org_names
    filter and `all_orgs` was never narrowed. The original listing case asserted this only
    from a NON-superadmin session, so a green run read as proof of a fix that covered half
    the cases.

    2026-09-20: the super-admin and the back-office were removed, so no principal can
    short-circuit the filter and there is no move control to render. The case is kept
    rather than deleted, because what it really pins is that /users exposes NO cross-org
    surface at all, and that property outlived the account that threatened it.

    The `is_superadmin` fixture is deliberately gone: writing it here would be exactly what
    verify_multiorg's dormancy ratchet exists to forbid."""
    r = client.get("/users")
    assert r.status_code == 200
    ctx = r.context
    assert list(ctx["org_names"]) == [aid], ctx["org_names"]
    assert "all_orgs" not in ctx, "the cross-org picker collection is gone"
    assert "can_move_org" not in ctx, "the cross-org move flag is gone"
    assert "Foreign Org Hidden" not in r.text
    assert "move-org" not in r.text, "a cross-org move control rendered"


def ldap_refusal_reaches_the_person():
    """GATE R3 MED-1. The login route deliberately SWALLOWS a hard LDAP refusal and falls
    through to local password auth. That is right for the two pre-existing refusals, where
    the person still has a local password to fall through TO. An unprovisioned directory
    user has none, so the fall-through ran `security.authenticate` against a password they
    do not have: a CORRECT directory password counted as a failed login, and the fifth
    attempt returned 429 with a 899-second lockout while the explanation was never shown.

    The refusal must reach the person, and must not count as a guess."""
    fresh = TestClient(app)
    csrf_for(fresh)
    with (
        patch.object(ldap_auth, "enabled", return_value=True),
        patch.object(ldap_auth, "try_login", return_value=(None, ldap_auth.NOT_PROVISIONED)),
        # The password step reserves an attempt up front and counts a wrong password ONLY
        # through settle_login_failure (the old record_login_failure is gone, TOTP gate
        # round 3 LOW-7): an unprovisioned refusal must never settle.
        patch.object(security, "settle_login_failure", side_effect=AssertionError("counted")),
    ):
        r = fresh.post(
            "/auth/login",
            data={"email": "unprovisioned@dir.test", LOGIN_FIELD: "correct-directory-pw"},
            follow_redirects=False,
        )
    assert r.status_code == 403, r.status_code
    # ...and the up-front reservation was given back: nothing counts against the pair.
    assert not security._failures.get(security._lockout_key("unprovisioned@dir.test", "testclient"))
    assert "not provisioned" in r.text, "the person was never told why"
    # The two PRE-EXISTING hard refusals must still fall through to local auth, which is
    # the behaviour the swallow exists for. Distinguishing them is the whole fix.
    assert ldap_auth.is_not_provisioned(ldap_auth.NOT_PROVISIONED)
    assert not ldap_auth.is_not_provisioned("LDAP identity may not take over a local admin")
    assert not ldap_auth.is_not_provisioned(None)


def cli_create_refuses_orgless_operator(aid):
    """GATE R3 MED-2. The OPERATOR door had the same hole as the web door and the two JIT
    doors: `--operator` resolves op_org_id to None, so under an active gate it minted an
    org_id=NULL account. Unlike a READ, "no org" has no meaning for a CREATE, so
    access.UNSCOPED is not the answer either. An operator must name the org."""
    from app.cli import _common
    from app.cli._common import CliError, Emitter
    from app.cli.cmd_user import _user_create

    class Args:
        def __init__(self, **kw):
            self.email = "operator-made@home.test"
            self.name = "Operator Made"
            self.role = Role.TRANSLATOR
            self.password = PASSWORD
            self.customer = None
            self.department = None
            self.remote = None
            self.org = None
            self.operator = False
            self.__dict__.update(kw)

    emit = Emitter(quiet=True)
    # The operator door itself is not under test here, so its key check is stubbed out;
    # what IS under test is what the door does once it has authorized an UNSCOPED run.
    with patch.object(_common, "_verify_operator_key", lambda: None):
        try:
            _user_create(Args(operator=True), emit)
        except CliError as exc:
            assert "--org" in str(exc), str(exc)
        else:
            raise AssertionError("--operator minted an org-less account under an active gate")
        made = select(User).where(User.email == "operator-made@home.test")
        with Session(engine) as s:
            assert s.exec(made).first() is None, "the refusal still wrote a row"
        # GATE R4 MED-1: the invite door had the same hole with a nastier shape. The CLI
        # exited 0 and printed a working-looking link, the accept page rendered with the
        # invitee's own address, and only the POST failed closed. The refusal reached the
        # wrong person, too late, with the wrong reason.
        from app.cli.cmd_user import _user_invite

        try:
            _user_invite(Args(operator=True, role=Role.TRANSLATOR), emit)
        except CliError as exc:
            assert "--org" in str(exc), str(exc)
        else:
            raise AssertionError("--operator issued an invitation that can never be accepted")
        # Naming the org still works, so this is a refusal to guess, not a lost capability.
        _user_create(Args(org=str(aid)), emit)
    with Session(engine) as s:
        row = s.exec(select(User).where(User.email == "operator-made@home.test")).first()
    assert row is not None and row.org_id == aid, getattr(row, "org_id", "missing")


def pre_gate_invite_is_dead_not_live_looking(client):
    """GATE R5 MED-2. An invitation issued BEFORE the gate was turned on carries
    org_id=NULL. After the flip the accept GET still rendered 200 with the invitee's own
    address on it, while _invite_scope hid the row from both the pending and the expired
    list on any ORG-SCOPED admin's page.

    GATE R6 LOW-1 corrects the first draft of this comment, which said no admin could see
    it at all. An OPERATOR can: `pending_invites(None)` returns those rows and
    `cli user invite-revoke --operator` revokes them. What was true is that the admin
    looking at the customer app, which is who would notice, could not.

    GATE R6 MED-1 corrects the other half. "It could never be accepted" was true of only
    one of accept_invite's two branches: the org check sat after the existing-account
    split, so an org-less invitation addressed to a pre-created account completed end to
    end and set that account's password. The check now runs before the split.

    The GET and the POST agree, so the door is not an oracle for who was invited.

    GATE R7 LOW-2: this case MUTATES state (it writes a User row and two Invite rows), so
    it is only safe where it sits, after the listing cases that assert exact totals. If it
    is ever moved earlier, `listing` starts failing on a count and the cause will not be
    obvious. Keep it after them, or give it its own emails and stop asserting totals."""
    from app.models import Invite

    with Session(engine) as s:
        inv = Invite(
            email="pre-gate-invitee@home.test",
            role=Role.TRANSLATOR,
            token="pre-gate-token-fixture",  # pregate: allow-secret offline fixture
            created_by="admin@home.test",
            expires_at=datetime.utcnow() + timedelta(days=7),
            org_id=None,
        )
        s.add(inv)
        s.commit()
    assert security.get_invite("pre-gate-token-fixture") is None, (
        "an org-less invite is still resolvable under an active gate"
    )
    page = client.get("/auth/accept/pre-gate-token-fixture", follow_redirects=False)
    assert "pre-gate-invitee@home.test" not in page.text, (
        "the accept page named the invitee on an invitation that can never be accepted"
    )
    # GATE R6 MED-1: the service must refuse it too, not only the route. accept_invite
    # split on "does this email already have a User row" BEFORE checking the org, so an
    # org-less invitation aimed at a pre-created account set that account's password. Drive
    # the service directly, past the route that happens to call get_invite first.
    with Session(engine) as s:
        pre = User(email="pre-made@home.test", org_id=None, active=False)
        s.add(pre)
        s.commit()
        inv2 = Invite(
            email="pre-made@home.test",
            role=Role.TRANSLATOR,
            token="pre-gate-existing-fixture",  # pregate: allow-secret offline fixture
            created_by="admin@home.test",
            expires_at=datetime.utcnow() + timedelta(days=7),
            org_id=None,
        )
        s.add(inv2)
        s.commit()
        pre_id, inv2_id = pre.id, inv2.id
    assert security.accept_invite("pre-gate-existing-fixture", "Pre Made", PASSWORD) is None, (
        "an org-less invitation set the password on a pre-created account"
    )
    with Session(engine) as s:
        row = s.get(User, pre_id)
        assert not row.password_hash and not row.active, "the account was activated anyway"
        assert not s.get(Invite, inv2_id).accepted, "the invitation was consumed anyway"
    # An invite that DOES carry an org is unaffected, so this is not a blanket refusal.
    good = security.create_invite("good-invitee@home.test", Role.TRANSLATOR, "admin@home.test")
    assert security.get_invite(good.token) is not None, "a scoped invite must still resolve"
    accepted = security.accept_invite(good.token, "Good Invitee", PASSWORD)
    assert accepted is not None and accepted.org_id is not None, "a scoped invite must accept"


def jit_provisioning_refuses_orgless(aid):
    """GATE R2 MED-1. The org stamp was fixed on ONE creation door. Re-deriving the family
    from app/ finds eleven call sites reaching create_user, and two more minted org-less
    accounts: OIDC (`get_or_create_sso_user`) and LDAP (`get_or_create_ldap_user`). Their
    SAML sibling sixty lines away already refused outright under the gate, so the remedy
    was written down in the same file and two doors did not have it.

    This train is what makes the orphan unreachable rather than merely odd: with the
    listing correctly scoped, a NULL-org account is invisible, unsearchable, refused by
    every management route, and cannot authenticate. A clean refusal at the door is better.

    An EXISTING account must still sign in, so the refusal covers creation only."""
    from app.services import ldap_auth

    doors = [
        (
            "sso",
            lambda: security.get_or_create_sso_user(
                {
                    "iss": "https://idp.test",
                    "sub": "s-1",
                    "email": "sso@idp.test",
                    "email_verified": True,
                }
            ),
        ),
        ("ldap", lambda: ldap_auth.get_or_create_ldap_user("ldap@dir.test", "LDAP User")),
        ("saml", lambda: security.get_or_create_saml_user("saml@idp.test", "SAML User")),
    ]
    for name, call in doors:
        user, error = call()
        assert user is None, f"{name} JIT provisioning minted an account under the gate"
        assert error and "not provisioned" in error, (name, error)
    # The refusal is about CREATION. An account that already exists still authenticates.
    known = security.create_user("known@idp.test", "Known", Role.TRANSLATOR, "", org_id=aid)
    user, error = security.get_or_create_sso_user(
        {"iss": "https://idp.test", "sub": "s-2", "email": known.email, "email_verified": True}
    )
    assert user is not None and error is None, (user, error)
    assert user.org_id == aid


def web_create_stamps_org(client, aid):
    """GATE R1 MED-2. POST /users called create_user with no org_id, so the service stored
    NULL. This train made that orphan far worse: before, the unscoped listing happened to
    show the account so an admin could still repair it. Once the listing is correctly
    scoped, a NULL-org account is invisible, unsearchable, refused by every management
    route and unable to authenticate.

    Driven end to end, because the defect only shows across create -> list -> manage."""
    email = "stamped@home.test"
    r = client.post(
        "/users",
        data={"email": email, "name": "Stamped", "role": Role.TRANSLATOR, LOGIN_FIELD: PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with Session(engine) as s:
        row = s.exec(select(User).where(User.email == email)).first()
    assert row is not None, "the account was not created"
    assert row.org_id == aid, f"created with org_id={row.org_id!r}, expected {aid}"
    listing = client.get("/users?q=Stamped")
    assert [u.email for u in listing.context["users"]] == [email], "the new account is invisible"
    assert (
        client.post(
            f"/users/{row.id}/department", data={"department": "changed"}, follow_redirects=False
        ).status_code
        == 303
    ), "the new account is unmanageable"
    fresh = TestClient(app)
    csrf_for(fresh)
    login = fresh.post(
        "/auth/login", data={"email": email, LOGIN_FIELD: PASSWORD}, follow_redirects=False
    )
    assert login.status_code == 303, "the new account cannot authenticate"


def foreign_search(client):
    r = client.get("/users?q=Foreign&page=999")
    assert r.status_code == 200
    assert r.context["users"] == [] and r.context["total"] == 0
    assert "Foreign Org Hidden" not in r.context["org_names"].values()


def main():
    logging.disable(logging.CRITICAL)
    client, uid, aid, bid, cid = setup()
    cases = []
    for http in (True, False):
        for planner in (False, True, "dispatch"):
            cases.append((f"mcp_{http}_{planner}", lambda h=http, p=planner: mcp_read(h, p)))
    for name in OPERATIONS:
        cases.append((f"cross_org_{name}", lambda n=name: operation(client, bid, cid, n, 404)))
    for name in ("role", "toggle"):
        cases.append((f"last_admin_{name}", lambda n=name: last_admin(client, uid, n)))
    for query in ("", "?page=2", "?q=Match", "?q=Match&page=2"):
        cases.append((f"listing{query}", lambda q=query: listing(client, aid, q)))
    for name in OPERATIONS:
        cases.append((f"same_org_{name}", lambda n=name: operation(client, aid, cid, n, 303)))
    cases.extend(
        [
            ("service_scopes", lambda: service_scopes(aid, bid, cid)),
            ("startup_transport", startup_transport),
            ("foreign_search", lambda: foreign_search(client)),
            ("listing_no_cross_org", lambda: listing_has_no_cross_org_surface(client, aid)),
            ("web_create_stamps_org", lambda: web_create_stamps_org(client, aid)),
            ("jit_provisioning_refuses_orgless", lambda: jit_provisioning_refuses_orgless(aid)),
            ("ldap_refusal_reaches_the_person", ldap_refusal_reaches_the_person),
            ("pre_gate_invite_is_dead", lambda: pre_gate_invite_is_dead_not_live_looking(client)),
            ("cli_create_orgless_operator", lambda: cli_create_refuses_orgless_operator(aid)),
        ]
    )
    print("collected ids: " + ", ".join(name for name, _ in cases))
    failures = []
    for name, case in cases:
        try:
            case()
            print(f"PASS {name}")
        except Exception as exc:
            failures.append(name)
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    assert not failures, failures
    print("SEC PREENABLE OK")


if __name__ == "__main__":
    main()
