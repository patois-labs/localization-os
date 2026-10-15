"""Round-3 #61/#62: in-process CLI multi-org isolation on EVERY identity/config leaf.

The single systematic root cause: `require_org_scope` (app/cli/_common.py) validated
--org and recorded args._op_org_id, but almost no command BODY applied it. Only the
GDPR export/erase twins + apikey-list scoped. So a non-operator caller passing
`--org <any-existing-org-id>` (the value need not even match the target) read/wrote
across orgs:

  * #61 user/apikey/member: list foreign users (PII), show/role/department/enable/
    disable/reset-password a foreign user, revoke a foreign key, and — the HEADLINE —
    MINT a working lsu_ key for another org's ADMIN.
  * #62 engine family: fully ungated (no require_org_scope at all) — list/show/update/
    delete/test operated across ALL orgs (base_url rewrite = a live content-exfil
    primitive; delete = cross-org destruction). customer list passed the gate then
    ignored the scope.

This suite PINS the closure on every leaf (foreign target == a clean not-found, no
data, no minting, no mutation), the positive controls (own-org works; operator key
still crosses orgs; gate off is byte-identical), and a RECURRENCE GUARD that
enumerates every scope-taking leaf in the user/apikey/member/engine groups from the
real parser and asserts each is classified (so a NEW command cannot silently
reintroduce the class).

ASCII-only; standalone one-process-per-suite (LOC_TMS_DATA_DIR = throwaway temp dir,
provider keys blanked -> MockProvider, deterministic + free).

Run standalone: python tests/verify_cli_org_scope_leaves.py  ->  prints "... OK".
"""

import hashlib
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_org_leaves_")

from sqlmodel import Session, select

from app.cli.__main__ import build_parser
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    ApiKey,
    Customer,
    EngineProfile,
    Organization,
    Project,
    Role,
    User,
)
from app.services import apikeys, settings


# --------------------------------------------------------------------------- #
# CLI drivers (in-process; capture exit code + stdout + stderr).
def _cli(argv, env=None):
    old = {}
    if env:
        for k, v in env.items():
            old[k] = os.environ.get(k)
            os.environ[k] = v
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli_main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return code, out.getvalue(), err.getvalue()


def _cli_json(argv, env=None):
    code, out, err = _cli(["--json"] + argv, env=env)  # noqa: RUF005
    payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    return code, payload, err


_OP_SECRET = "operator-secret-value"


def _gate_on():
    settings.set_many(
        {
            "security_auth_required": "1",
            "multi_org_enabled": "1",
            "operator_key_hash": hashlib.sha256(_OP_SECRET.encode()).hexdigest(),
        }
    )
    settings.invalidate_cache()


def _gate_off():
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


# init_db() does NOT wipe (create_all is idempotent) and User.email has no DB-level
# UNIQUE, so repeated _seed() calls in one process would accumulate DUPLICATE-email
# rows; a by-email lookup would then return a stale row from a PRIOR test's org and a
# scoped positive control would spuriously 404. Each seed uses a per-call counter to
# keep every org/user/customer/engine unique + isolated across tests in this process.
_SEED_N = 0


def _seed():
    """Two orgs A and B, each fully populated (unique per call). Returns ids/emails."""
    global _SEED_N
    _SEED_N += 1
    n = _SEED_N
    init_db()
    with Session(engine) as s:
        oa = Organization(name=f"Leaf Org A{n}")
        ob = Organization(name=f"Leaf Org B{n}")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        # users
        a_user = User(email=f"a-user-{n}@a.test", name="A User", role=Role.TRANSLATOR, org_id=oa.id)
        b_user = User(email=f"b-user-{n}@b.test", name="B User", role=Role.TRANSLATOR, org_id=ob.id)
        b_admin = User(email=f"b-admin-{n}@b.test", name="B Admin", role=Role.ADMIN, org_id=ob.id)
        for u in (a_user, b_user, b_admin):
            s.add(u)
        # customers
        a_cust = Customer(name=f"A Customer {n}", org_id=oa.id)
        b_cust = Customer(name=f"B Customer {n}", org_id=ob.id)
        s.add(a_cust)
        s.add(b_cust)
        # projects
        a_proj = Project(name=f"A Project {n}", org_id=oa.id)
        b_proj = Project(name=f"B Project {n}", org_id=ob.id)
        s.add(a_proj)
        s.add(b_proj)
        # engines (seeded directly so org_id is set; unique names)
        a_eng = EngineProfile(
            name=f"eng-a-{n}", kind="local", base_url="http://a-eng", org_id=oa.id
        )
        b_eng = EngineProfile(
            name=f"eng-b-{n}", kind="local", base_url="http://b-eng-orig", org_id=ob.id
        )
        s.add(a_eng)
        s.add(b_eng)
        s.commit()
        for obj in (a_user, b_user, b_admin, a_cust, b_cust, a_proj, b_proj, a_eng, b_eng):
            s.refresh(obj)
        ids = {
            "org_a": oa.id,
            "org_b": ob.id,
            "a_uid": a_user.id,
            "a_email": a_user.email,
            "b_uid": b_user.id,
            "b_email": b_user.email,
            "b_admin_uid": b_admin.id,
            "b_admin_email": b_admin.email,
            "a_cust": a_cust.id,
            "a_cust_name": a_cust.name,
            "b_cust": b_cust.id,
            "b_cust_name": b_cust.name,
            "a_proj": a_proj.id,
            "b_proj": b_proj.id,
            "a_eng": a_eng.id,
            "b_eng": b_eng.id,
        }
    # a key owned by B's user (so revoke/revoke-all have a cross-org target)
    b_key, _ = apikeys.create_key(ids["b_uid"], name="b key", scopes="docs")
    ids["b_key_id"] = b_key.id
    return ids


# --------------------------------------------------------------------------- #
# #61 -- user family: every foreign-org target is a clean 'no such user' (exit 2),
# no PII, no mutation. mirror-uniform with a truly-missing id.
def test_user_family_scoped():
    d = _seed()
    _gate_on()
    try:
        a = str(d["org_a"])
        b_uid = str(d["b_uid"])
        missing = "987654"
        leaves = [
            ["user", "show", b_uid],
            ["user", "role", b_uid, "viewer"],
            ["user", "department", b_uid, "sales"],
            ["user", "customer", b_uid, "--customer", str(d["a_cust"])],
            ["user", "enable", b_uid],
            ["user", "disable", b_uid, "--yes"],
            ["user", "reset-password", b_uid, "--password", "fixture-abc-123456"],
            # TOTP train: both resolve through _resolve_user_scoped before any prompt.
            ["user", "mfa-status", b_uid],
            ["user", "unlock", b_uid],
            ["user", "mfa-reset", b_uid, "--yes"],
        ]
        for verb in leaves:
            code_x, out_x, err_x = _cli(["--org", a] + verb)  # noqa: RUF005
            assert code_x == 2, (verb, "foreign target must exit 2", code_x, err_x)
            assert f"no such user: {b_uid}" in err_x, (verb, err_x)
            # No foreign PII on ANY stream (the confirm() email-oracle risk).
            for stream in (out_x, err_x):
                assert d["b_email"] not in stream, (verb, "foreign email leaked")
            # Byte-uniform with a truly-missing id (modulo the uid).
            code_m, _out_m, err_m = _cli(
                ["--org", a] + [x if x != b_uid else missing for x in verb]
            )
            assert code_m == 2, (verb, code_m, err_m)
            assert err_m.replace(missing, "N") == err_x.replace(b_uid, "N"), (
                verb,
                "missing vs cross-org must be indistinguishable",
                err_m,
                err_x,
            )
        # The foreign user survives untouched (role, active, no password set).
        with Session(engine) as s:
            bu = s.get(User, d["b_uid"])
            assert bu is not None and bu.role == Role.TRANSLATOR and bu.active, bu
        # POSITIVE: an own-org target works.
        code, pay, err = _cli_json(["--org", a, "user", "show", str(d["a_uid"])])
        assert code == 0 and pay["ok"] and pay["email"] == d["a_email"], (code, pay, err)
        print(
            "  #61 user: show/role/department/customer/enable/disable/reset-password/mfa-* "
            "on a foreign user == missing (exit 2, no PII, untouched); own-org OK"
        )
    finally:
        _gate_off()


def test_user_list_scoped():
    d = _seed()
    _gate_on()
    try:
        code, pay, err = _cli_json(["--org", str(d["org_a"]), "user", "list"])
        assert code == 0, (code, err)
        emails = {u["email"] for u in pay["users"]}
        assert d["a_email"] in emails, ("own-org user missing", emails)
        assert d["b_email"] not in emails and d["b_admin_email"] not in emails, (
            "#61: user list leaked a foreign org's users",
            emails,
        )
        print("  #61 user list: --org confines the enumeration (foreign users excluded)")
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# #61 (gate finding 1): a merely-scoped, NON-operator `user create` must STAMP the
# new account into op_org_id. Pre-fix it landed org_id=None, which
# access.current_org_id -> None, which browsable_customers/llmemory_view/org_id_clause
# treat as a SEE-ALL super-scope -> a scoped caller mints a see-all admin (re-opens
# the #61 read leak). The gate's read predicates DISAGREE on org_id=None (see-all vs
# blocked), so the stamp is the load-bearing fix on the write side.
def test_invites_scoped():
    from app.services import security

    d = _seed()
    _gate_on()
    try:
        code, pay, err = _cli_json(
            [
                "--org",
                str(d["org_a"]),
                "user",
                "invite",
                "--email",
                "own-invite@a.test",
                "--role",
                "translator",
            ]
        )
        assert code == 0, (pay, err)
        own = security.get_invite(pay["token"])
        assert own.created_by == "cli"
        foreign = security.create_invite("foreign-invite@b.test", "translator", d["b_email"])
        scope = ["--org", str(d["org_a"])]
        code, pay, err = _cli_json([*scope, "user", "invites"])
        assert code == 0, err
        assert own.id in {i["id"] for i in pay["invites"]}
        assert own.org_id == d["org_a"]
        assert own.id not in {i.id for i in security.pending_invites(d["org_b"])}
        assert own.id not in {i.id for i in security.pending_invites(None)}
        assert foreign.id not in {i["id"] for i in pay["invites"]}
        assert own.token not in json.dumps(pay) and foreign.token not in json.dumps(pay)
        code, pay, err = _cli_json([*scope, "user", "invite-revoke", "--id", str(foreign.id)])
        assert code == 2 and security.get_invite(foreign.token) is not None
        code, pay, err = _cli_json([*scope, "user", "invite-revoke", "--id", str(own.id)])
        assert code == 0 and security.get_invite(own.token) is None, err
        # #181: create_invite now refuses an org-less invitation under the gate, so the
        # legacy NULL-org row this case needs is written as the pre-#181 data it models.
        from datetime import datetime, timedelta

        from app.models import Invite

        with Session(engine) as s:
            null = Invite(
                email="null-invite@x.test",
                role="translator",
                token="null-invite-legacy-fixture",  # pregate: allow-secret offline fixture
                created_by="system",
                org_id=None,
                expires_at=datetime.utcnow() + timedelta(days=7),
            )
            s.add(null)
            s.commit()
            s.refresh(null)
        assert null.id in {i.id for i in security.pending_invites(None)}
        assert null.id not in {i.id for i in security.pending_invites(d["org_a"])}
        assert security.revoke_invite(null.id, d["a_email"], org_id=d["org_a"]) is None
        assert security.revoke_invite(null.id, "cli", org_id=None) is not None
        expired = security.create_invite(
            "expired-own@a.test", "translator", "cli", days=-1, org_id=d["org_a"]
        )
        foreign_expired = security.create_invite(
            "expired-foreign@b.test", "translator", "cli", days=-1, org_id=d["org_b"]
        )
        code, pay, err = _cli_json([*scope, "user", "invites", "--expired"])
        assert code == 0 and expired.id in {i["id"] for i in pay["invites"]}, err
        assert foreign_expired.id not in {i["id"] for i in pay["invites"]}
        assert expired.token not in json.dumps(pay)
        # SPEC-expired-invite-delete: invite-revoke deletes an EXPIRED invite in scope
        # only; a foreign org's expired invite is the same exit-2 not-found.
        code, pay, err = _cli_json(
            [*scope, "user", "invite-revoke", "--id", str(foreign_expired.id)]
        )
        assert code == 2, (pay, err)
        assert foreign_expired.id in {i.id for i in security.expired_invites(d["org_b"])}
        code, pay, err = _cli_json([*scope, "user", "invite-revoke", "--id", str(expired.id)])
        assert code == 0 and pay["expired"] is True, (pay, err)
        assert expired.id not in {i.id for i in security.expired_invites(d["org_a"])}
    finally:
        _gate_off()


def test_invite_supersede_scoped():
    from app.models import AuditLog
    from app.services import security

    d = _seed()
    _gate_on()
    try:
        foreign = security.create_invite("same-invite@x.test", "translator", d["b_email"])
        own = security.create_invite("same-invite@x.test", "translator", d["a_email"])
        assert security.get_invite(foreign.token) is not None, "supersede crossed orgs"
        replacement = security.create_invite(" SAME-INVITE@x.test ", "translator", d["a_email"])
        assert security.get_invite(own.token) is None
        assert security.get_invite(foreign.token) is not None
        assert security.get_invite(replacement.token) is not None
        with Session(engine) as s:
            events = s.exec(
                select(AuditLog).where(
                    AuditLog.action == "user.invite.supersede", AuditLog.target == own.email
                )
            ).all()
            assert len(events) == 1 and events[0].actor == d["a_email"]
        # #181: an org-less invitation is now refused outright under the gate, so it can
        # supersede nothing (the stronger form of what this step used to check).
        try:
            security.create_invite(own.email, "translator", "cli", org_id=None)
        except security.OrglessAccountError:
            pass
        else:
            raise AssertionError("an org-less invitation was issued under the gate")
        assert security.get_invite(replacement.token) is not None
        assert security.get_invite(foreign.token) is not None
        _gate_off()
        assert {foreign.id, replacement.id} <= {i.id for i in security.pending_invites(d["org_a"])}
        # Gate off, an org-less invite is allowed and supersedes unscoped, as before.
        null = security.create_invite(own.email, "translator", "cli", org_id=None)
        assert security.get_invite(null.token) is not None
        security.create_invite(own.email, "translator", "system", org_id=None)
        assert all(security.get_invite(i.token) is None for i in (foreign, replacement, null))
    finally:
        _gate_off()


def test_user_create_stamps_scope():
    from app.services import access

    d = _seed()
    _gate_on()
    try:
        a = str(d["org_a"])
        code, pay, err = _cli_json(
            [
                "--org",
                a,
                "user",
                "create",
                "--email",
                "minted-admin@a.test",
                "--role",
                "admin",
                "--password",
                "fixture-abc-123456",
            ]
        )
        assert code == 0 and pay["ok"], (code, pay, err)
        with Session(engine) as s:
            mu = s.exec(select(User).where(User.email == "minted-admin@a.test")).first()
            assert mu is not None, "created user vanished"
            assert mu.org_id == d["org_a"], (
                "#61 finding 1: scoped `user create` did NOT stamp op_org_id "
                "(landed a see-all account)",
                mu.org_id,
            )
            # current_org_id must NOT be None (None == see-all on the read surfaces).
            assert access.current_org_id(mu) == d["org_a"], (
                "current_org_id must be the scoped org, not the see-all None",
                access.current_org_id(mu),
            )
        # SEC-T5 GATE R4 HIGH-1 / R5 MED-1: this leg used to assert the OPPOSITE, that
        # `user create --operator` succeeds under an active gate and leaves org_id=None.
        #
        # What is TRUE of such an account, all four driven: it cannot log in under an
        # active gate, it is absent from the scoped /users listing, every /users/{id}
        # route 404s it, and any API key it owns answers 401.
        #
        # What an earlier version of this comment ALSO claimed, and which is FALSE:
        # that nobody could repair it. `cli user list --operator` enumerates it with its
        # id, `cli user org <user> <org> --operator` moves it into an org in one command
        # (after which it signs in), and the back office does the same over HTTP.
        # docs/CLI.md:1245 documents that repair verbatim. So this WAS step one of a real
        # two-step operator loop, and saying otherwise was the same reason-from-what-is-
        # in-front-of-you error the rest of this audit is about.
        #
        # The refusal stays anyway, on the narrower ground that survives the correction:
        # `user create --org <id>` does the whole job in one step and needs no operator
        # key, so the two-step loop buys nothing and its first step leaves an account that
        # cannot be used until someone remembers the second. Refusing to guess an org is
        # cheap here; the positive leg below proves the capability is intact.
        code, pay, err = _cli_json(
            [
                "--operator",
                "user",
                "create",
                "--email",
                "global-user@x.test",
                "--role",
                "translator",
                "--password",
                "fixture-abc-123456",
            ],
            env={"LOC_TMS_OPERATOR_KEY": _OP_SECRET},
        )
        assert code != 0, ("--operator must not mint an org-less account", code, pay, err)
        assert "--org" in (err or "") + str(pay), (err, pay)
        with Session(engine) as s:
            gu = s.exec(select(User).where(User.email == "global-user@x.test")).first()
            assert gu is None, ("the refusal still wrote a row", getattr(gu, "org_id", None))
        # POSITIVE: naming the org works, so only the guess is refused.
        code, pay, err = _cli_json(
            [
                "--org",
                str(d["org_a"]),
                "user",
                "create",
                "--email",
                "named-org-user@a.test",
                "--role",
                "translator",
                "--password",
                "fixture-abc-123456",
            ],
            env={"LOC_TMS_OPERATOR_KEY": _OP_SECRET},
        )
        assert code == 0 and pay["ok"], (code, pay, err)
        with Session(engine) as s:
            nu = s.exec(select(User).where(User.email == "named-org-user@a.test")).first()
            assert nu is not None and nu.org_id == d["org_a"], (
                "--org must still stamp the named org",
                getattr(nu, "org_id", None),
            )
        print(
            "  #61 finding 1: scoped `user create` stamps op_org_id (no see-all "
            "account); `--operator` create is REFUSED rather than minting an org-less "
            "account, and `--org` still stamps"
        )
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# #61 HEADLINE: minting an lsu_ key for a FOREIGN org's admin is refused and NO key
# is created.
def test_apikey_issue_foreign_admin_refused():
    d = _seed()
    _gate_on()
    try:
        a = str(d["org_a"])
        # (the foreign admin identified by the caller's own --user value; the refusal
        # echoing that value back is not a NEW disclosure -- it is the caller's input.)
        code, pay, err = _cli_json(
            [
                "--org",
                a,
                "apikey",
                "issue",
                "--user",
                d["b_admin_email"],
                "--scopes",
                "docs keys admin",
            ]
        )
        assert code == 2, ("minting a foreign-admin key must be refused", code, pay, err)
        assert not pay.get("ok") and "no such user" in (pay.get("error") or "").lower(), (pay, err)
        # PROVE no key was minted for B's admin.
        with Session(engine) as s:
            n = len(s.exec(select(ApiKey).where(ApiKey.user_id == d["b_admin_uid"])).all())
            assert n == 0, ("a foreign-admin key WAS minted (the #61 headline)", n)
        # POSITIVE: issuing for the operator's OWN-org user mints a key.
        code, pay, err = _cli_json(
            ["--org", a, "apikey", "issue", "--user", d["a_email"], "--scopes", "docs"]
        )
        assert code == 0 and pay["ok"] and pay["api_key"].startswith("lsu_"), (code, pay, err)
        print(
            "  #61 HEADLINE: apikey issue for a foreign-org ADMIN refused, NO key "
            "minted; own-org issue works"
        )
    finally:
        _gate_off()


def test_apikey_revoke_and_revoke_all_scoped():
    d = _seed()
    _gate_on()
    try:
        a = str(d["org_a"])
        # revoke-by-id of a foreign org's key -> 404-equivalent, key stays active.
        code, _out, err = _cli(["--org", a, "apikey", "revoke", str(d["b_key_id"])])
        assert code == 2 and "no such api key" in err.lower(), (code, err)
        with Session(engine) as s:
            assert s.get(ApiKey, d["b_key_id"]).active, "foreign key was revoked (#61)"
        # revoke-all for a foreign user -> refused, key survives.
        code, _out, err = _cli(["--org", a, "apikey", "revoke-all", str(d["b_uid"]), "--yes"])
        assert code == 2 and "no such user" in err.lower(), (code, err)
        with Session(engine) as s:
            assert s.get(ApiKey, d["b_key_id"]).active, "foreign key mass-revoked (#61)"
        print("  #61 apikey revoke/revoke-all: foreign key/user refused, key intact")
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# #61 member: neither a foreign user nor a foreign project can be touched.
def test_member_family_scoped():
    d = _seed()
    _gate_on()
    try:
        a = str(d["org_a"])
        # add a FOREIGN user to my own project -> no such user.
        code, _o, err = _cli(["--org", a, "member", "add", str(d["b_uid"]), str(d["a_proj"])])
        assert code == 2 and "no such user" in err.lower(), (code, err)
        # add my own user to a FOREIGN project -> no such project.
        code, _o, err = _cli(["--org", a, "member", "add", str(d["a_uid"]), str(d["b_proj"])])
        assert code == 2 and "no such project" in err.lower(), (code, err)
        # remove against a foreign project -> no such project (before confirm).
        code, _o, err = _cli(
            ["--org", a, "member", "remove", str(d["a_uid"]), str(d["b_proj"]), "--yes"]
        )
        assert code == 2 and "no such project" in err.lower(), (code, err)
        # list a foreign project's members -> no such project (no email enumeration).
        code, _o, err = _cli(["--org", a, "member", "list", str(d["b_proj"])])
        assert code == 2 and "no such project" in err.lower(), (code, err)
        print("  #61 member: foreign user/project add/remove/list refused (no oracle)")
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# #62 engine family: list confined; show/update/delete/test on a foreign engine are
# 404; the foreign engine's base_url is NOT rewritten and the row is NOT deleted.
def test_engine_family_scoped():
    d = _seed()
    _gate_on()
    try:
        a = str(d["org_a"])
        b_eng = str(d["b_eng"])
        # list is confined to the scoped org.
        code, pay, err = _cli_json(["--org", a, "engine", "list"])
        assert code == 0, (code, err)
        eng_ids = {e["id"] for e in pay["engines"]}
        assert d["a_eng"] in eng_ids and d["b_eng"] not in eng_ids, (
            "#62: engine list leaked a foreign org's engine",
            eng_ids,
        )
        # show/update/delete/test on the foreign engine -> no such engine.
        for verb in (
            ["engine", "show", b_eng],
            ["engine", "update", b_eng, "--base-url", "http://attacker.example"],
            ["engine", "delete", b_eng, "--yes"],
            ["engine", "test", b_eng],
            ["engine", "inspect", b_eng],
            [
                "engine",
                "jev-try",
                b_eng,
                "--source",
                "a",
                "--target",
                "b",
                "--source-locale",
                "en",
                "--target-locale",
                "de",
            ],
        ):
            code, _o, err = _cli(["--org", a] + verb)  # noqa: RUF005
            assert code == 2 and f"no such engine: {b_eng}" in err, (verb, code, err)
        # PROVE the foreign engine is untouched: base_url unchanged AND row survives.
        with Session(engine) as s:
            be = s.get(EngineProfile, d["b_eng"])
            assert be is not None, "#62: foreign engine was DELETED cross-org"
            assert be.base_url == "http://b-eng-orig", (
                "#62: foreign engine base_url was REWRITTEN cross-org",
                be.base_url,
            )
        # POSITIVE: own-org engine show + update work.
        code, pay, err = _cli_json(["--org", a, "engine", "show", str(d["a_eng"])])
        assert code == 0 and pay["ok"], (code, pay, err)
        code, pay, err = _cli_json(
            ["--org", a, "engine", "update", str(d["a_eng"]), "--base-url", "http://a-eng-new"]
        )
        assert code == 0 and pay["ok"], (code, pay, err)
        with Session(engine) as s:
            assert s.get(EngineProfile, d["a_eng"]).base_url == "http://a-eng-new"
        print(
            "  #62 engine: list confined; show/update/delete/test on a foreign engine "
            "== missing (exit 2), base_url intact + row survives; own-org OK"
        )
    finally:
        _gate_off()


def test_customer_list_scoped():
    d = _seed()
    _gate_on()
    try:
        code, pay, err = _cli_json(["--org", str(d["org_a"]), "customer", "list"])
        assert code == 0, (code, err)
        names = {c["name"] for c in pay["customers"]}
        assert d["a_cust_name"] in names, ("own-org customer missing", names)
        assert d["b_cust_name"] not in names, (
            "#62: customer list leaked a foreign org's customer",
            names,
        )
        print("  #62 customer list: --org confines the enumeration (foreign excluded)")
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# The sanctioned operator key STILL crosses orgs unscoped (the gate exists to gate
# THAT, not to break it).
def test_operator_key_still_cross_org():
    d = _seed()
    _gate_on()
    try:
        env = {"LOC_TMS_OPERATOR_KEY": _OP_SECRET}
        # user show across orgs with --operator.
        code, pay, err = _cli_json(["--operator", "user", "show", str(d["b_uid"])], env=env)
        assert code == 0 and pay["ok"] and pay["email"] == d["b_email"], (code, pay, err)
        # engine show across orgs with --operator.
        code, pay, err = _cli_json(["--operator", "engine", "show", str(d["b_eng"])], env=env)
        assert code == 0 and pay["ok"], (code, pay, err)
        # user list unscoped shows BOTH orgs.
        code, pay, err = _cli_json(["--operator", "user", "list"], env=env)
        emails = {u["email"] for u in pay["users"]}
        assert d["a_email"] in emails and d["b_email"] in emails, emails
        print("  operator key: --operator still reads/manages cross-org (unscoped) OK")
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# Gate OFF (single-tenant): no --org needed, scoping is inert -> byte-identical.
def test_gate_off_byte_identical():
    d = _seed()
    _gate_off()
    try:
        # user show works with NO --org.
        code, pay, err = _cli_json(["user", "show", str(d["b_uid"])])
        assert code == 0 and pay["ok"], (code, pay, err)
        # engine list shows every engine.
        code, pay, err = _cli_json(["engine", "list"])
        ids = {e["id"] for e in pay["engines"]}
        assert d["a_eng"] in ids and d["b_eng"] in ids, ids
        # customer list shows every customer.
        code, pay, err = _cli_json(["customer", "list"])
        names = {c["name"] for c in pay["customers"]}
        assert d["a_cust_name"] in names and d["b_cust_name"] in names, names
        # apikey issue works with no --org.
        code, pay, err = _cli_json(["apikey", "issue", "--user", d["a_email"]])
        assert code == 0 and pay["api_key"].startswith("lsu_"), (code, pay, err)
        print(
            "  gate-off: user/engine/customer/apikey run with NO --org "
            "(byte-identical single-tenant)"
        )
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# #73 round-4 (six/seven-helper family): the glossary/content-type/style-guide LIST +
# show/get CLI paths now feed op_org_SCOPE (not op_org_id) to the retrofitted read
# helpers, which fail closed on a bare None under an active gate. This pins the COUPLING:
#   * the key-verified --operator door (op_org_scope -> access.UNSCOPED) still lists /
#     reads ACROSS orgs (SEES rows, not an empty list) -- a regression here would mean a
#     bare None slipped through and the operator got a fail-closed empty result;
#   * a scoped --org caller sees only its own org;
#   * a NON-operator scopeless call is still REFUSED (exit 2) by require_org_scope.
def _seed_module_assets(d):
    """Add one customer-scoped glossary + content type + org-global style guide to each
    of the two orgs seeded by _seed(). Returns the new ids merged into d."""
    from app.models import (
        ContentType,
        Glossary,
        GlossaryStatus,
        StyleGuide,
        StyleGuideStatus,
    )

    with Session(engine) as s:
        ga = Glossary(
            name=f"gloss-a-{_SEED_N}",
            status=GlossaryStatus.DRAFT,
            org_id=d["org_a"],
            customer_id=d["a_cust"],
        )
        gb = Glossary(
            name=f"gloss-b-{_SEED_N}",
            status=GlossaryStatus.DRAFT,
            org_id=d["org_b"],
            customer_id=d["b_cust"],
        )
        ca = ContentType(name=f"ct-a-{_SEED_N}", org_id=d["org_a"], customer_id=d["a_cust"])
        cb = ContentType(name=f"ct-b-{_SEED_N}", org_id=d["org_b"], customer_id=d["b_cust"])
        sa = StyleGuide(
            name=f"sg-a-{_SEED_N}", status=StyleGuideStatus.DRAFT, org_id=d["org_a"], body="a"
        )
        sb = StyleGuide(
            name=f"sg-b-{_SEED_N}", status=StyleGuideStatus.DRAFT, org_id=d["org_b"], body="b"
        )
        for obj in (ga, gb, ca, cb, sa, sb):
            s.add(obj)
        s.commit()
        for obj in (ga, gb, ca, cb, sa, sb):
            s.refresh(obj)
        d.update(
            {
                "gloss_a": ga.id,
                "gloss_b": gb.id,
                "ct_a": ca.id,
                "ct_b": cb.id,
                "sg_a": sa.id,
                "sg_b": sb.id,
            }
        )
    return d


def test_sixhelper_operator_door_lists_cross_org():
    d = _seed_module_assets(_seed())
    _gate_on()
    try:
        env = {"LOC_TMS_OPERATOR_KEY": _OP_SECRET}
        a = str(d["org_a"])
        # --operator: SEES both orgs' glossaries (the coupling proof -- NOT an empty list).
        code, pay, err = _cli_json(["--operator", "glossary", "module", "list"], env=env)
        assert code == 0, (code, err)
        gids = {g["id"] for g in pay["glossaries"]}
        assert {d["gloss_a"], d["gloss_b"]} <= gids, (
            "#73 coupling: --operator glossary list must see BOTH orgs (op_org_scope -> "
            "UNSCOPED); an empty/partial list means a bare None fail-closed the operator",
            gids,
        )
        # --operator content-type + style-guide lists ALSO sweep both orgs.
        code, pay, err = _cli_json(["--operator", "content-type", "list"], env=env)
        assert code == 0 and {d["ct_a"], d["ct_b"]} <= {c["id"] for c in pay["content_types"]}, (
            code,
            pay,
            err,
        )
        code, pay, err = _cli_json(["--operator", "style-guide", "list"], env=env)
        assert code == 0 and {d["sg_a"], d["sg_b"]} <= {g["id"] for g in pay["style_guides"]}, (
            code,
            pay,
            err,
        )
        # --operator SHOW reads a foreign org's glossary (visible -> UNSCOPED).
        code, pay, err = _cli_json(
            ["--operator", "glossary", "module", "show", str(d["gloss_b"])], env=env
        )
        assert code == 0 and pay.get("ok") and pay.get("id") == d["gloss_b"], (code, pay, err)
        # POSITIVE scoped: --org A sees ONLY org A's glossary.
        code, pay, err = _cli_json(["--org", a, "glossary", "module", "list"])
        assert code == 0, (code, err)
        gids = {g["id"] for g in pay["glossaries"]}
        assert d["gloss_a"] in gids and d["gloss_b"] not in gids, (
            "--org A glossary list must be confined to org A",
            gids,
        )
        print(
            "  #73 coupling: --operator glossary/content-type/style-guide list+show sweep "
            "cross-org (op_org_scope->UNSCOPED); --org confines to one org"
        )
    finally:
        _gate_off()


def test_sibling_create_operator_door_resolves_content_type():
    """#73 round-4 RESIDUAL coupling: the CLI CREATE commands (glossary module / style
    guide) validate their --content-type through content_types.attachable, which now fails
    closed on a bare None under an active gate. They therefore feed op_org_SCOPE (not
    op_org_id): the --operator door hands attachable access.UNSCOPED so a cross-org create
    still RESOLVES the type, while the persisted row's org is collapsed back to None. If a
    bare None had slipped through, the create would fail closed and exit 2."""
    from app.models import Glossary, StyleGuide

    d = _seed_module_assets(_seed())
    _gate_on()
    try:
        env = {"LOC_TMS_OPERATOR_KEY": _OP_SECRET}
        cust_a, ct_a = str(d["a_cust"]), str(d["ct_a"])
        # --operator glossary create WITH a content type: resolves (UNSCOPED) + stamps None.
        code, pay, err = _cli_json(
            [
                "--operator",
                "glossary",
                "module",
                "create",
                "OpGloss",
                "--customer",
                cust_a,
                "--content-type",
                ct_a,
            ],
            env=env,
        )
        assert code == 0 and pay.get("ok"), (
            "#73 coupling: --operator glossary create with a content type must SUCCEED "
            "(op_org_scope->UNSCOPED resolves it); exit 2 means a bare None fail-closed it",
            code,
            pay,
            err,
        )
        with Session(engine) as s:
            g = s.get(Glossary, pay["id"])
            assert g is not None and g.org_id is None and g.content_type_id == d["ct_a"], (
                "#73 coupling: operator-created glossary must stamp org None + resolve the type",
                getattr(g, "org_id", "gone"),
                getattr(g, "content_type_id", None),
            )
        # --operator style-guide create WITH a content type: same coupling.
        code, pay, err = _cli_json(
            [
                "--operator",
                "style-guide",
                "create",
                "OpSG",
                "--customer",
                cust_a,
                "--content-type",
                ct_a,
            ],
            env=env,
        )
        assert code == 0 and pay.get("ok"), (code, pay, err)
        with Session(engine) as s:
            sg = s.get(StyleGuide, pay["id"])
            assert sg is not None and sg.org_id is None and sg.content_type_id == d["ct_a"], (
                "#73 coupling: operator-created style guide must stamp org None + resolve type",
                getattr(sg, "org_id", "gone"),
                getattr(sg, "content_type_id", None),
            )
        print(
            "  #73 coupling: --operator glossary/style-guide create resolves the content "
            "type cross-org (op_org_scope->UNSCOPED) + stamps org None"
        )
    finally:
        _gate_off()


def test_sibling_template_apply_operator_coupling_and_forged_key():
    """GATE CATCH (the caller the coupling sweep missed) + the forged-operator control.

    `template apply` reaches content.project_create with creator=op_creator() (None on
    the --operator door) AND org_id=op_org_id() (also None there), so the applied
    project's INHERITED content type validated against a bare None and the whole apply
    failed closed once attachable was guarded. cmd_template now threads op_org_scope, so
    a key-verified --operator apply still resolves the type.

    The FORGED control: the same command with a WRONG (and with an ABSENT)
    LOC_TMS_OPERATOR_KEY never reaches op_org_scope at all -- require_org_scope refuses
    at the key check (exit 2) and creates nothing."""
    from app.models import Customer, Project
    from app.services import content as content_svc
    from app.services import templates as tsvc

    d = _seed_module_assets(_seed())
    n = _SEED_N
    with Session(engine) as s:
        cust = s.get(Customer, d["a_cust"])
        cust.default_content_type_id = d["ct_a"]
        s.add(cust)
        s.commit()
        seed_p = content_svc.project_create(
            s,
            f"tpl-seed-{n}",
            source_locale="en-US",
            target_locales="es-ES",
            customer_id=d["a_cust"],
            content_type_id=d["ct_a"],
        )
        tpl_id = tsvc.save_as_template(s, seed_p.id, f"tpl-op-{n}", actor="probe").id
    _gate_on()
    try:
        args = [
            "template",
            "apply",
            str(tpl_id),
            "--name",
            f"tpl-op-proj-{n}",
            "--customer-id",
            str(d["a_cust"]),
        ]
        # (a) FORGED operator: wrong key, then no key at all -- refused before any work.
        for env, why in (
            ({"LOC_TMS_OPERATOR_KEY": "not-the-key"}, "wrong key"),
            ({"LOC_TMS_OPERATOR_KEY": ""}, "absent key"),
        ):
            code, _out, err = _cli(["--operator", *args], env=env)
            assert code == 2, (why, "forged --operator must exit 2", code, err)
        with Session(engine) as s:
            assert (
                s.exec(select(Project).where(Project.name == f"tpl-op-proj-{n}")).first() is None
            ), "forged --operator must create NOTHING"
        # (b) the real door: key-verified --operator resolves the inherited content type.
        code, pay, err = _cli_json(["--operator", *args], env={"LOC_TMS_OPERATOR_KEY": _OP_SECRET})
        assert code == 0 and pay.get("ok"), (
            "#73 coupling: `template apply --operator` must SUCCEED (op_org_scope->"
            "UNSCOPED resolves the customer's inherited content type)",
            code,
            pay,
            err,
        )
        with Session(engine) as s:
            proj = s.get(Project, pay["project_id"])
            assert proj is not None and proj.content_type_id == d["ct_a"], (
                "#73 coupling: the applied project must carry the inherited type",
                getattr(proj, "content_type_id", None),
            )
        print(
            "  #73 coupling: `template apply --operator` resolves the inherited content "
            "type; a forged/absent operator key is refused and creates nothing"
        )
    finally:
        _gate_off()


def test_sixhelper_scopeless_nonoperator_refused():
    _seed_module_assets(_seed())
    _gate_on()
    try:
        # No --org, no --operator, gate ON: require_org_scope REFUSES (exit 2), unchanged.
        for verb in (
            ["glossary", "module", "list"],
            ["content-type", "list"],
            ["style-guide", "list"],
        ):
            code, _out, err = _cli(verb)
            assert code == 2, (verb, "scopeless non-operator must exit 2", code, err)
            assert "multi-org mode is ON" in err, (verb, err)
        print("  #73 coupling: scopeless NON-operator list still REFUSED (exit 2), unchanged")
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# RECURRENCE GUARD: enumerate every scope-taking leaf in the user/apikey/member/
# engine groups from the REAL parser and assert each is CLASSIFIED. A new command
# in these families that is neither drive-tested (SCOPE_ENFORCED) nor explicitly
# EXEMPT fails this test, so the class cannot silently return.
#
# SCOPE_ENFORCED: leaves that must NOT let a merely-scoped caller reach across orgs.
#   Most are drive-tested above with a foreign TARGET (refused == not-found). Three
#   (apikey list, user export, user erase) are genuinely scoped in SIBLING suites
#   (verify_multiorg / verify_admin_org_scope) rather than re-driven here; `user
#   create` STAMPS op_org_id (test_user_create_stamps_scope) so a scoped caller
#   cannot mint a see-all (org_id=None) account.
# EXEMPT: leaves with NO cross-org existing TARGET to leak AND no org-stamp to make
#   -- documented, each with a reason. A future command may only join EXEMPT with cause.
SCOPE_ENFORCED = frozenset(
    {
        "user create",  # STAMPS op_org_id (no see-all account)
        "user list",
        "user invites",
        "user invite-revoke",
        "user show",
        "user role",
        "user department",
        "user customer",
        "user enable",
        "user disable",
        "user reset-password",
        "user mfa-status",  # TOTP train: foreign target pinned in test_user_family_scoped
        "user mfa-reset",
        "user unlock",
        "user export",
        "user erase",  # scoped in sibling suites (the model this fix followed)
        "apikey issue",
        "apikey list",
        "apikey revoke",
        "apikey revoke-all",
        "member add",
        "member remove",
        "member list",
        "engine list",
        "engine show",
        "engine update",
        "engine delete",
        "engine test",
        "engine models",  # #346: _get_profile(id, op_org_id) scope first, like engine test
        "engine inspect",  # Jev Train C: _get_profile scope first, then the judge check
        "engine jev-try",
    }
)
EXEMPT = {
    "user invite": "creates a NEW invite (no target to read; org-stamp n/a to invites)",
    "user org": "require_operator: cross-org move is operator-only by design",
    "engine create": "creates a NEW engine (stamped into op_org_id, no foreign target)",
    "engine import-jev-env": "creates a NEW hosted Jev engine from the host env key (stamped "
    "into op_org_id; its idempotence check reads only the scoped org's engines)",
}
_SCOPE_GROUPS = ("user", "apikey", "member", "engine")


def _leaf_paths(parser, prefix):
    """Every dispatchable leaf command under `parser`, space-joined onto `prefix`. RECURSES through
    nested subparser groups (a two-level group would otherwise be classified as one GROUP and its
    leaves never seen). A leaf is a (sub)parser with no further subparsers action."""
    subs = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
    if not subs:
        yield prefix
        return
    for sub in subs:
        for name, child in sub.choices.items():
            yield from _leaf_paths(child, f"{prefix} {name}")


def _group_leaf_paths(parser, group):
    """The space-joined leaf paths under one top-level group (e.g. 'user show'), recursing
    through any nested subparser groups."""
    for action in parser._actions:
        if action.__class__.__name__ != "_SubParsersAction":
            continue
        grp = action.choices.get(group)
        if grp is None:
            continue
        yield from _leaf_paths(grp, group)


def test_recurrence_guard_every_scope_leaf_classified():
    parser = build_parser()
    actual = set()
    for g in _SCOPE_GROUPS:
        actual |= set(_group_leaf_paths(parser, g))
    assert actual, "parser introspection found no leaves (harness broke)"
    classified = SCOPE_ENFORCED | set(EXEMPT)
    unclassified = sorted(actual - classified)
    assert not unclassified, (
        "NEW scope-taking CLI leaf(s) with no org-scope classification -- each MUST "
        "either apply the --org filter (add to SCOPE_ENFORCED + a foreign-target pin "
        "above) or be justified in EXEMPT:\n  " + "\n  ".join(unclassified)
    )
    # And the table cannot rot: every SCOPE_ENFORCED/EXEMPT entry must still resolve
    # to a real leaf (a renamed/removed command trips this).
    stale = sorted((SCOPE_ENFORCED | set(EXEMPT)) - actual)
    assert not stale, (
        "classification names a leaf that no longer exists (rename/"
        "remove without updating the guard):\n  " + "\n  ".join(stale)
    )
    # Self-check: the guard is discriminating (a fabricated leaf is NOT classified).
    assert "user definitely-not-real" not in classified
    print(
        "  RECURRENCE GUARD: all %d scope-taking user/apikey/member/engine leaves "  # noqa: UP031
        "classified (SCOPE_ENFORCED=%d, EXEMPT=%d)"
        % (len(actual), len(SCOPE_ENFORCED), len(EXEMPT))
    )


def main():
    test_user_family_scoped()
    test_user_list_scoped()
    test_invites_scoped()
    test_invite_supersede_scoped()
    test_user_create_stamps_scope()
    test_apikey_issue_foreign_admin_refused()
    test_apikey_revoke_and_revoke_all_scoped()
    test_member_family_scoped()
    test_engine_family_scoped()
    test_customer_list_scoped()
    test_operator_key_still_cross_org()
    test_gate_off_byte_identical()
    test_sixhelper_operator_door_lists_cross_org()
    test_sibling_create_operator_door_resolves_content_type()
    test_sibling_template_apply_operator_coupling_and_forged_key()
    test_sixhelper_scopeless_nonoperator_refused()
    test_recurrence_guard_every_scope_leaf_classified()
    print("CLI ORG SCOPE LEAVES OK")


if __name__ == "__main__":
    main()
