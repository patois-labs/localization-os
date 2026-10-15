"""Trust-host-auth verification (offline) — composable-core phase 0, Train 0b.

Regression guard for the embedded trust-host-auth door: under the DOUBLE CONDITION
(a boot-time `LOC_TMS_EMBEDDED=1` boot AND the `embedded_trust_host_auth` Setting ==
"1") a TOKENLESS `/api` request resolves to a configured trust principal instead of a
401. The reviewer attacks the auth boundary hardest, so this suite proves each law:

  * all FOUR embedded x switch combinations (this process is EMBEDDED, toggling the
    switch; a sibling child process is NON-embedded, proving it ignores the switch);
  * TWO INDEPENDENT LEVERS — SCOPES (LOC_TMS_TRUST_SCOPES) gate the scope-checked
    surface; the RBAC ROLE (LOC_TMS_TRUST_ROLE, default viewer) gates the role-checked
    surface. A DEFAULT (viewer) principal is 403 on the admin-role escape
    (`POST /api/apikeys/issue`), a manage route, and a write route; the admin surface
    is reachable ONLY by an explicit role=admin + admin-scope opt-in — and even then the
    irreversible verbs keep their `remote_admin_enabled` lock (404);
  * the role<->scope COUPLING LAW: role=admin without the admin scope REFUSES at boot;
  * a BAD/nonempty token is still 401 (trust never launders a wrong token), and a
    request with a VALID token uses the TOKEN identity, not trust;
  * the trust principal is a DEDICATED identity: an existing NON-trust row is NEVER
    adopted (boot refuses), a superseded principal is DEACTIVATED on rotation, and the
    SSO/LDAP login seams REFUSE to adopt a trust-source row;
  * the email is format-validated at boot (a CRLF/malformed value refuses);
  * the immutable-key law covers `embedded_trust_host_auth`;
  * audit attribution: a trust-mode mutation is audited to the principal's REAL email
    and is DISTINGUISHABLE (an `(auth=trust)` action marker);
  * multi-org: the principal binds to ONE configured org or the boot REFUSES;
  * FAIL LOUDLY AT BOOT, and fail CLOSED if the switch is flipped on after boot;
  * non-enumerability: the switch-off / non-embedded 401 body is byte-identical.

Boot-simulation pattern (verify_docker_auth / verify_embedded_profile). Hermetic:
throwaway LOC_TMS_DATA_DIR + cleared provider keys, never the real DB. ASCII-only
prints. Final line: TRUST HOST AUTH OK.
"""

import contextlib
import logging
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

# Two modes, decided from argv BEFORE any app import (config freezes the mode at import).
_NONEMBEDDED_CHILD = "--nonembedded-child" in sys.argv

if _NONEMBEDDED_CHILD:
    init_env("loctms_trust_ne_")
    os.environ.pop("LOC_TMS_EMBEDDED", None)
    os.environ["LOC_TMS_SERVER"] = "1"  # a NORMAL, non-embedded server boot
else:
    init_env("loctms_trust_")
    os.environ.pop("LOC_TMS_SERVER", None)
    os.environ["LOC_TMS_EMBEDDED"] = "1"

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.main import app
from app.models import Organization, Role, User
from app.services import (
    apikeys,
    audit,
    gdpr,
    ldap_auth,
    security,
    settings,
    trust_auth,
)

PRINCIPAL = "host-service@embedder.example"
#: A >= 32-char shared secret for the (b) source gate (avoids the weak-secret warning).
SECRET = "trust-proxy-secret-" + "a" * 24


def _is(r, code, what):
    assert r.status_code == code, f"{what}: expected {code}, got {r.status_code} ({r.text[:300]})"


def _set(**kv) -> None:
    settings.set_many({k: v for k, v in kv.items()})  # noqa: C416
    settings.invalidate_cache()


def _clear_trust_env() -> None:
    for k in (
        "LOC_TMS_TRUST_PRINCIPAL",
        "LOC_TMS_TRUST_SCOPES",
        "LOC_TMS_TRUST_ROLE",
        "LOC_TMS_TRUST_ORG_ID",
        "LOC_TMS_TRUST_HEADER_SECRET",
        "LOC_TMS_TRUST_HEADER_SECRETS",
    ):
        os.environ.pop(k, None)


def _configure(
    principal=PRINCIPAL,
    scopes=None,
    role=None,
    org_id=None,
    header_secret=None,
    header_secrets=None,
):
    """Set the trust env then run the boot-time resolver+provisioner. `header_secret` sets the
    SINGULAR literal env; `header_secrets` sets the PLURAL comma-separated accept-list env."""
    _clear_trust_env()
    if principal is not None:
        os.environ["LOC_TMS_TRUST_PRINCIPAL"] = principal
    if scopes is not None:
        os.environ["LOC_TMS_TRUST_SCOPES"] = scopes
    if role is not None:
        os.environ["LOC_TMS_TRUST_ROLE"] = role
    if org_id is not None:
        os.environ["LOC_TMS_TRUST_ORG_ID"] = str(org_id)
    if header_secret is not None:
        os.environ["LOC_TMS_TRUST_HEADER_SECRET"] = header_secret
    if header_secrets is not None:
        os.environ["LOC_TMS_TRUST_HEADER_SECRETS"] = header_secrets
    trust_auth.configure_at_boot()


class _WarnCapture(logging.Handler):
    """Collect messages emitted on the loc_tms.trust_auth logger (M-8 capture)."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _capture_trust_warnings():
    lg = logging.getLogger("loc_tms.trust_auth")
    handler = _WarnCapture()
    prev_level = lg.level
    lg.setLevel(logging.WARNING)
    lg.addHandler(handler)
    try:
        yield handler.messages
    finally:
        lg.removeHandler(handler)
        lg.setLevel(prev_level)


class _StubHeaders:
    # NIT-10: real Starlette lowercases only the ingested keys and matches a
    # canonical-cased query; this stub lowercases BOTH sides. Immaterial here — every
    # call queries by the exact canonical header name and stores by the same name, so the
    # result set is identical to Starlette's. (The non-ASCII/latin-1 wire path is proven
    # end-to-end with a real bytes header in arm [15], not through this stub.)
    def __init__(self, pairs):
        self._pairs = pairs

    def getlist(self, name):
        return [v for k, v in self._pairs if k.lower() == name.lower()]


class _StubRequest:
    """A minimal request exposing headers.getlist(), for direct annotate/source calls
    where httpx would reject the header at the client (e.g. a CRLF value)."""

    def __init__(self, pairs):
        self.headers = _StubHeaders(pairs)


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except trust_auth.TrustConfigError:
        return True


# --------------------------------------------------------------------------- #
# The NON-embedded sibling: proves the switch is INERT off an embedded boot.
# --------------------------------------------------------------------------- #
def nonembedded_child_main() -> None:
    assert config.IS_EMBEDDED is False, "child must NOT be embedded"
    with TestClient(app) as c:
        # Even with the switch ON and the full trust env set, a non-embedded boot must
        # ignore trust entirely: active() is False, configure is a no-op, tokenless 401.
        _set(embedded_trust_host_auth="1")
        _configure()
        assert trust_auth.active() is False, "non-embedded: active() must be False"
        assert trust_auth.principal() is None, "non-embedded: principal() must be None"
        r = c.get("/api/projects")
        _is(r, 401, "non-embedded switch-on tokenless")
        assert r.json() == {"ok": False, "error": "unauthorized"}, r.text
        _set(embedded_trust_host_auth="0")
        assert c.get("/api/projects").status_code == 401, "non-embedded switch-off tokenless"
    print(
        "  [child] non-embedded: switch on OR off -> active() False, tokenless 401 "
        "(byte-identical), the switch is inert off an embedded boot"
    )
    print("NONEMBEDDED CHILD OK")


# --------------------------------------------------------------------------- #
# The EMBEDDED boot: this process.
# --------------------------------------------------------------------------- #
def embedded_main() -> None:
    assert config.IS_EMBEDDED is True, "LOC_TMS_EMBEDDED=1 must set IS_EMBEDDED"

    # The immutable-key law: the new perimeter key is host-only, refused over /api.
    assert "embedded_trust_host_auth" in settings.REMOTE_IMMUTABLE_KEYS, (
        "the trust switch must be on REMOTE_IMMUTABLE_KEYS (host-only)"
    )
    assert settings.DEFAULTS.get("embedded_trust_host_auth") == "0", "default must be OFF"
    assert "remote_admin_enabled" in settings.REMOTE_IMMUTABLE_KEYS
    print(
        "  [0] embedded_trust_host_auth: default '0', on REMOTE_IMMUTABLE_KEYS "
        "(refused over /api by the enumerating R3 suite); remote_admin_enabled also immutable"
    )

    with TestClient(app) as c:
        # A real per-user key + user, to prove the TOKEN path is untouched by trust mode.
        real = security.create_user("real@x.com", "Real", Role.ADMIN, "real-password-1234")
        _row, real_key = apikeys.create_key(real.id, "real", scopes="api docs")

        # ---- (embedded, switch OFF): tokenless -> 401, byte-identical -------------- #
        _set(embedded_trust_host_auth="0")
        _configure()
        assert trust_auth.active() is False and trust_auth.principal() is None
        r = c.get("/api/projects")
        _is(r, 401, "embedded switch-off tokenless")
        assert r.json() == {"ok": False, "error": "unauthorized"}, r.text
        assert "trust" not in r.text.lower() and "embedded" not in r.text.lower(), r.text
        print(
            "  [1] (embedded, switch OFF): tokenless -> 401 with the byte-identical "
            "{ok:false,error:unauthorized} body; nothing reveals the switch exists"
        )

        # ---- FAIL LOUD AT BOOT ----------------------------------------------------- #
        _set(embedded_trust_host_auth="1")
        assert _raises(lambda: _configure(principal=None)), "no principal must raise"
        assert _raises(lambda: _configure(principal="system")), "reserved id must raise"
        # MEDIUM 4: a CRLF/malformed email is format-refused BEFORE any row is written.
        assert _raises(lambda: _configure(principal="x@y.com\r\nactor=system")), (
            "a CRLF email must raise (never written as a row / audit actor)"
        )
        assert _raises(lambda: _configure(principal="not-an-email@")), "malformed email must raise"
        with Session(engine) as s:
            leaked = s.exec(select(User).where(User.email.like("%actor=system%"))).first()
        assert leaked is None, "a CRLF email leaked into a User row"
        # The role<->scope COUPLING LAW: role=admin without the admin scope refuses.
        assert _raises(lambda: _configure(scopes="api docs keys", role="admin")), (
            "role=admin without the admin scope must refuse at boot"
        )
        assert _raises(lambda: _configure(role="wizard")), "an unknown role must refuse"
        print(
            "  [2] fail-loud-at-boot: missing/reserved/CRLF/malformed principal, "
            "role=admin-without-admin-scope, and an unknown role each raise "
            "TrustConfigError -- never a silent request-time degrade"
        )

        # ---- (embedded, switch ON), DEFAULT principal = viewer role ---------------- #
        _configure()  # default scopes "api docs keys", default role viewer
        assert trust_auth.active() is True
        ident = trust_auth.principal()
        assert ident is not None and ident["kind"] == "user"
        assert ident["actor"] == PRINCIPAL and ident["auth_kind"] == "trust", ident
        assert apikeys.scope_set(ident["scopes"]) == {"api", "docs", "keys"}, ident
        prov = security.get_by_email(PRINCIPAL)
        assert prov is not None and prov.role == Role.VIEWER and prov.active, (
            f"the DEFAULT trust principal must be a least-privilege VIEWER: {prov!r}"
        )
        assert prov.auth_source == "trust", prov
        # A tokenless READ works (docs scope, no role gate on the read).
        _is(c.get("/api/projects"), 200, "default principal tokenless read")
        print(
            f"  [3] (embedded, switch ON) default principal: real VIEWER-role User {PRINCIPAL!r}, "
            "scopes api/docs/keys; a docs READ returns 200"
        )

        # ---- THE ROLE LEVER: a default (viewer) principal reaches NO role-gated route #
        # This is the BLOCKER regression: with the old role=admin, ALL THREE were reachable.
        _is(
            c.post("/api/apikeys/issue", json={"scopes": "docs", "name": "x"}),
            403,
            "default principal must be 403 on apikeys/issue (is_admin gate) -- the escape",
        )
        _is(
            c.post("/api/projects", json={"name": "x", "targets": "fr-FR"}),
            403,
            "default principal must be 403 on a write route (_write_block)",
        )
        _is(
            c.post("/api/customers", json={"name": "x"}),
            403,
            "default principal must be 403 on a manage route (_manage_block)",
        )
        print(
            "  [4] ROLE lever: a DEFAULT (viewer) principal is 403 on apikeys/issue, "
            "a write route, and a manage route -- the admin-role escape is CLOSED"
        )

        # ---- THE ADMIN OPT-IN: role=admin + admin scope reaches them, but the -------- #
        #      remote_admin_enabled verbs STILL 404 --------------------------------- #
        _configure(scopes="api docs keys admin", role="admin")
        assert security.get_by_email(PRINCIPAL).role == Role.ADMIN
        _is(
            c.post("/api/apikeys/issue", json={"scopes": "docs", "name": "k"}),
            200,
            "admin principal reaches apikeys/issue",
        )
        _is(
            c.post("/api/projects", json={"name": "P", "targets": "fr-FR"}),
            200,
            "admin principal reaches a write route",
        )
        _is(
            c.post("/api/customers", json={"name": "Cust"}),
            200,
            "admin principal reaches a manage route",
        )
        _set(remote_admin_enabled="0")
        _is(
            c.post("/api/admin/config", json={"settings": {"org_name": "x"}}),
            404,
            "an R3 verb is 404 even for a trust ADMIN while remote_admin_enabled is off",
        )
        print(
            "  [5] admin opt-in (role=admin + admin scope): apikeys/issue + write + "
            "manage all pass, but the remote_admin_enabled R3 verb is still 404"
        )

        # ---- THE SCOPE LEVER (independent of role, on a read) ---------------------- #
        _configure(scopes="api docs keys", role="viewer")
        _is(c.get("/api/projects"), 200, "docs scope present -> read 200")
        _configure(scopes="api", role="viewer")
        _is(c.get("/api/projects"), 403, "docs scope absent -> read 403 (scope honoured)")
        # The admin-scope tick: absent without the admin scope, present with it.
        _configure(scopes="api docs keys", role="viewer")
        _is(c.post("/api/admin/tick"), 403, "tick without the admin scope -> 403")
        _configure(scopes="api docs keys admin", role="admin")
        _is(c.post("/api/admin/tick"), 200, "tick with the admin scope+role -> 200")
        print(
            "  [6] SCOPE lever: dropping docs 403s the docs read; the admin-scope tick "
            "is 403 without the admin scope and 200 with it"
        )

        # ---- bad token still 401; a valid token uses the TOKEN identity ------------ #
        _configure()  # back to the default principal
        _is(
            c.get("/api/projects", headers={"X-Loc-Token": "lsu_totally-bogus-token"}),
            401,
            "bad token under trust mode",
        )
        # A whitespace-only header is treated as tokenless (NIT 8): resolves to trust.
        _is(
            c.get("/api/projects", headers={"X-Loc-Token": "   "}),
            200,
            "a whitespace-only token header is tokenless -> trust read 200",
        )
        # A VALID token resolves to the TOKEN's user, NOT the trust principal.
        _is(c.get("/api/projects", headers={"X-Loc-Token": real_key}), 200, "valid token")
        print(
            "  [7] bad token -> 401 (trust never launders a wrong token); a "
            "whitespace-only header is tokenless; a VALID token uses its own user"
        )

        # ---- audit attribution + distinguishability -------------------------------- #
        _configure(scopes="api docs keys", role="translator")
        before = _max_audit_id()
        # A mutating /api request is audited after call_next regardless of the route's own
        # status, so the attribution invariant holds whether or not this create is accepted.
        c.post("/api/projects", json={"name": "Trust Made Me", "targets": "fr-FR"})
        rows = [rec for rec in audit.iter_export() if _rec_id(rec) > before]
        trust_rows = [
            x
            for x in rows
            if x.get("actor") == PRINCIPAL and "POST /api/projects" in x.get("action", "")
        ]
        assert trust_rows, f"no audit row attributed to the trust principal: {rows!r}"
        assert all("(auth=trust)" in x["action"] for x in trust_rows), trust_rows
        assert all(x["actor"] == PRINCIPAL for x in trust_rows), trust_rows
        print(
            f"  [8] audit: a trust-mode mutation is attributed to the real email {PRINCIPAL!r} and "
            "is distinguishable by the (auth=trust) action marker"
        )

        # ---- HIGH 2: a NON-trust row is NEVER adopted ------------------------------ #
        security.create_user("human@corp.com", "Human", Role.VIEWER, "human-password-1234")
        assert _raises(lambda: _configure(principal="human@corp.com")), (
            "configuring a trust principal onto an existing LOCAL account must refuse"
        )
        h = security.get_by_email("human@corp.com")
        assert h.auth_source == "local" and h.role == Role.VIEWER and h.active, (
            f"the refused adoption must have left the human account untouched: {h!r}"
        )
        print(
            "  [9] HIGH 2: an existing non-trust (human/local) row is NEVER adopted -- "
            "the boot refuses and the account is left byte-identical"
        )

        # ---- HIGH 3a: rotation DEACTIVATES the superseded principal ----------------- #
        _configure(principal="principal-a@embedder.example")
        _configure(principal="principal-b@embedder.example")
        a = security.get_by_email("principal-a@embedder.example")
        b = security.get_by_email("principal-b@embedder.example")
        assert a is not None and not a.active, "the superseded principal A must be deactivated"
        assert b is not None and b.active, "the current principal B must be active"
        print(
            "  [10] HIGH 3a: rotating LOC_TMS_TRUST_PRINCIPAL deactivates the prior "
            "principal (no dormant live admins accumulate)"
        )

        # ---- gate sso-localpw round 3: a hand-edited " Trust " row ------------------ #
        def _hand_edit(email, source, active):
            with Session(engine) as s:
                row = s.exec(select(User).where(User.email == email)).first()
                row.auth_source, row.active = source, active
                s.add(row)
                s.commit()

        # LOW-1: boot REFUSES to re-assert (and so re-activate) a non-canonical trust row.
        _hand_edit("principal-a@embedder.example", " Trust ", False)
        try:
            _configure(principal="principal-a@embedder.example")
            refused = ""
        except trust_auth.TrustConfigError as exc:
            refused = str(exc)
        assert "not exactly 'trust'" in refused, refused
        a = security.get_by_email("principal-a@embedder.example")
        assert (a.auth_source, a.active) == (" Trust ", False), (a.auth_source, a.active)
        # INFO-2: the superseded-principal cleanup matches the NORMALIZED value.
        _hand_edit("principal-a@embedder.example", " Trust ", True)
        _configure(principal="principal-b@embedder.example")
        a = security.get_by_email("principal-a@embedder.example")
        assert not a.active, "a hand-edited ' Trust ' superseded row must be deactivated"
        h = security.get_by_email("human@corp.com")
        assert h.active and h.auth_source == "local", "the cleanup never touches a real account"
        print(
            "  [10b] a hand-edited ' Trust ' principal row is refused at boot (left as is), "
            "and the rotation cleanup deactivates it when superseded"
        )

        # ---- HIGH 3b: the SSO + LDAP login seams REFUSE a trust-source row ---------- #
        _configure()  # provisions PRINCIPAL as a trust row (and re-activates it)
        sso_user, sso_err = security.get_or_create_sso_user({"email": PRINCIPAL})
        assert sso_user is None and "service identity" in (sso_err or ""), (sso_user, sso_err)
        ldap_user, ldap_err = ldap_auth.get_or_create_ldap_user(PRINCIPAL, "X")
        assert ldap_user is None and "service identity" in (ldap_err or ""), (ldap_user, ldap_err)
        print(
            "  [11] HIGH 3b: an SSO or LDAP login as the trust email is REFUSED -- a "
            "trust identity is not a login identity, so it can never be taken over"
        )

        # ---- flip-on-after-boot: fail CLOSED (no principal -> 401) ------------------ #
        saved = trust_auth._TRUST
        trust_auth._TRUST = None  # simulate: switch on, never provisioned
        assert trust_auth.active() is True
        assert trust_auth.principal() is None, "flip-on-after-boot must fail CLOSED"
        _is(c.get("/api/projects"), 401, "unprovisioned trust -> tokenless 401")
        trust_auth._TRUST = saved
        print(
            "  [12] flip-on-after-boot latch: switch on but not provisioned -> "
            "principal() None, tokenless 401 (fail closed)"
        )

        # ---- multi-org: bind to one org or REFUSE at boot -------------------------- #
        _set(multi_org_enabled="1")
        org_id = _an_org_id()
        assert _raises(lambda: _configure(org_id=None)), "multi-org + no org must refuse"
        assert _raises(lambda: _configure(org_id=9_000_001)), "multi-org + phantom org must refuse"
        _configure(org_id=org_id)
        ident = trust_auth.principal()
        assert ident is not None and ident["org_id"] == org_id, ident
        assert security.get_by_email(PRINCIPAL).org_id == org_id, "user row org mismatch"
        print(
            "  [13] multi-org: no binding or a phantom org REFUSES at boot; a valid "
            "LOC_TMS_TRUST_ORG_ID binds the principal to exactly that one org"
        )

        # ========================================================================= #
        # PHASE-3 HARDENING: (b) source-secret gate + (a) attribution annotation + (c).
        # Back to single-tenant so these arms read against the default org binding.
        # ========================================================================= #
        _set(multi_org_enabled="0", embedded_trust_host_auth="1")
        _hardening_arms(c)

        # Leave global state clean for any sibling suite sharing the interpreter.
        _clear_trust_env()
        _set(multi_org_enabled="0", embedded_trust_host_auth="0", remote_admin_enabled="0")

    # Prove the NON-embedded boot ignores the switch, via a sibling child process.
    child_env = dict(os.environ)
    child_env.pop("LOC_TMS_EMBEDDED", None)
    child_env.pop("LOC_TMS_DATA_DIR", None)  # the child's init_env mints its own
    for k in (
        "LOC_TMS_TRUST_PRINCIPAL",
        "LOC_TMS_TRUST_SCOPES",
        "LOC_TMS_TRUST_ROLE",
        "LOC_TMS_TRUST_ORG_ID",
        "LOC_TMS_TRUST_HEADER_SECRET",
    ):
        child_env.pop(k, None)
    child = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--nonembedded-child"],
        env=child_env,
        capture_output=True,
        text=True,
    )
    if child.returncode != 0:
        sys.stdout.write(child.stdout)
        sys.stderr.write(child.stderr)
        raise AssertionError("the non-embedded child boot FAILED (see output above)")
    for line in child.stdout.splitlines():
        if line.strip():
            print("  " + line.strip())

    print("TRUST HOST AUTH OK")


def _hardening_arms(c) -> None:
    """Phase-3 hardening arms (14-22, plus round-2-gate sub-arms 16b/19b/19c): (b)
    source-secret gate (incl. MED-3 non-ASCII boot refusal + F7 real-bytes latin-1 path),
    (a) attribution-only annotation hard-coupled to (b) with the on-behalf-of marker in the
    audit DETAIL column (HIGH-1) + the subject-access mask (MED-2), the F5/B-2 no-active()
    pin, and (c) the warnings (incl. the F6 weak-secret nudge + the LOW-8-ii drop
    diagnostic). Runs single-tenant under the armed double condition; each _configure
    re-freezes the boot state (incl. the secret and the reject/drop latches)."""
    UNAUTH = {"ok": False, "error": "unauthorized"}
    HDR = "X-Loc-Trust-Secret"
    USER_HDR = "X-Loc-Trust-User"

    # ---- [14] (b) secret honored ONLY when armed -------------------------------- #
    _configure(scopes="api docs keys", role="viewer")  # no secret => 0b
    _is(c.get("/api/projects"), 200, "no secret configured -> tokenless read 200 (0b)")
    _configure(scopes="api docs keys", role="viewer", header_secret=SECRET)
    _is(
        c.get("/api/projects", headers={HDR: SECRET}),
        200,
        "secret set + correct X-Loc-Trust-Secret -> 200",
    )
    r = c.get("/api/projects")
    _is(r, 401, "secret set + MISSING header -> 401")
    assert r.json() == UNAUTH, r.text
    r = c.get("/api/projects", headers={HDR: "wrong-" + SECRET})
    _is(r, 401, "secret set + WRONG header -> 401")
    assert r.json() == UNAUTH, r.text
    assert "trust" not in r.text.lower() and "secret" not in r.text.lower(), r.text
    print(
        "  [14] (b) source secret: unset -> 0b 200; set -> 200 ONLY with the correct "
        "X-Loc-Trust-Secret, else a byte-identical 401 (non-enumerable)"
    )

    # ---- [15] (b) hardening edges (H-2 / M-7 / MED-3) --------------------------- #
    _configure(scopes="api docs keys", role="viewer", header_secret=SECRET)
    # Non-ASCII header -> 401, END-TO-END through the real middleware/route stack (F7). httpx
    # rejects non-ASCII *str* header values (ascii codec) but ACCEPTS *bytes* values, so a
    # bytes header drives the exact Starlette latin-1-decoded path a real proxy/attacker
    # would hit: source_permitted must return False (a clean 401), never a compare_digest
    # TypeError -> unauthenticated 500 / is-armed enumeration oracle. (The earlier stub call
    # never crossed the ASGI decode; this proves H-2 on the real stack.)
    r = c.get("/api/projects", headers={HDR: bytes([0xE9]) * 40})
    _is(r, 401, "a non-ASCII (latin-1 0xE9) X-Loc-Trust-Secret header -> 401, never a 500")
    assert r.json() == UNAUTH, r.text
    # A 64 KB header -> 401 (TestClient's ASGI transport bypasses h11's ~16 KB header cap;
    # production answers 431 first -- this arm only proves the app never 500s on a huge value).
    _is(
        c.get("/api/projects", headers={HDR: "z" * 65536}),
        401,
        "64 KB X-Loc-Trust-Secret -> 401 (never a 500)",
    )
    # Two copies of the secret header -> 401 (a client dup riding the proxy's copy cannot
    # smuggle a value in -- getlist duplicate refusal).
    _is(
        c.get("/api/projects", headers=[(HDR, SECRET), (HDR, SECRET)]),
        401,
        "two X-Loc-Trust-Secret copies -> 401 (getlist duplicate refusal)",
    )
    # A blank-after-strip secret env -> treated as UNCONFIGURED (0b 200 + the c-boot warn).
    with _capture_trust_warnings() as msgs:
        _configure(scopes="api docs keys", role="viewer", header_secret="   ")
    _is(c.get("/api/projects"), 200, "blank-after-strip secret -> unconfigured, 0b 200")
    assert any("ARMED without LOC_TMS_TRUST_HEADER_SECRET" in m for m in msgs), msgs
    # MED-3: a non-ASCII CONFIGURED secret is REFUSED AT BOOT. Starlette decodes the wire
    # header latin-1, so a non-ASCII configured secret could NEVER match the re-encoded
    # header -- it would silently BRICK every tokenless /api request. The boot refuses it
    # loudly instead (fail-loud discipline). This replaces the earlier stub assertion that
    # compared a Python str to itself and never crossed the ASGI latin-1 decode.
    assert _raises(
        lambda: _configure(scopes="api docs keys", role="viewer", header_secret="ü" * 40)
    ), (
        "a non-ASCII configured LOC_TMS_TRUST_HEADER_SECRET must REFUSE at boot (never "
        "silently brick /api)"
    )
    # F-3: an interior ASCII CONTROL char is ASCII (isascii() True) but un-sendable in a real
    # header value; .strip() only trims the ends, so it survives, boots clean, then 401s
    # forever -- the same silent-brick class as MED-3, one input class over. Refuse at boot.
    assert _raises(
        lambda: _configure(
            scopes="api docs keys", role="viewer", header_secret="a" * 20 + "\x07" + "b" * 20
        )
    ), (
        "a LOC_TMS_TRUST_HEADER_SECRET with an interior ASCII control char must REFUSE at "
        "boot (isascii() True but not sendable -> silent brick if allowed to arm)"
    )
    print(
        "  [15] (b) edges: non-ASCII header -> 401 not 500 (real e2e bytes path); 64 KB "
        "-> 401; two copies -> 401; blank secret -> unconfigured (0b 200 + c-boot warn); a "
        "non-ASCII CONFIGURED secret REFUSES at boot (MED-3); an interior-control secret "
        "REFUSES at boot (F-3 -- no silent brick)"
    )

    # ---- [15b] (b) ROTATION accept-list via the PLURAL env (SPEC §4.5, MED-1) ---- #
    # LOC_TMS_TRUST_HEADER_SECRETS (PLURAL) is the COMMA-SEPARATED accept-list for zero-cutover
    # rotation: configure "old,new", migrate the proxy to inject "new", drop "old" next restart.
    # During the overlap BOTH values are honored (closing the R1-F9 rolling-restart 401 window).
    # The multiplicity lives on the APP's accept side; the proxy still sends exactly ONE header,
    # so M-7 (the §4.5 "two-value overlap" footgun) is untouched.
    OLD_SECRET = "rotate-old-secret-" + "o" * 20  # 38 chars, no comma
    NEW_SECRET = "rotate-new-secret-" + "n" * 20  # 38 chars, no comma
    _configure(scopes="api docs keys", role="viewer", header_secrets=OLD_SECRET + "," + NEW_SECRET)
    _is(
        c.get("/api/projects", headers={HDR: OLD_SECRET}),
        200,
        "rotation: the OLD accept-list secret is honored (overlap)",
    )
    _is(
        c.get("/api/projects", headers={HDR: NEW_SECRET}),
        200,
        "rotation: the NEW accept-list secret is honored (overlap)",
    )
    _is(
        c.get("/api/projects", headers={HDR: "rotate-unknown-" + "z" * 20}),
        401,
        "rotation: a value in NEITHER accept-list slot is rejected",
    )
    # M-7 survives the accept-list: two header copies (even both valid) => 401.
    _is(
        c.get("/api/projects", headers=[(HDR, OLD_SECRET), (HDR, NEW_SECRET)]),
        401,
        "rotation: two X-Loc-Trust-Secret copies still 401 (M-7 unchanged by the accept-list)",
    )
    # Blank elements (a trailing/double comma) are DROPPED -- never a phantom empty secret.
    _configure(scopes="api docs keys", role="viewer", header_secrets=OLD_SECRET + ",," + NEW_SECRET)
    _is(c.get("/api/projects", headers={HDR: OLD_SECRET}), 200, "old,,new -> old honored")
    _is(c.get("/api/projects", headers={HDR: NEW_SECRET}), 200, "old,,new -> new honored")
    _is(
        c.get("/api/projects", headers={HDR: "   "}),
        401,
        "a blank header never matches a dropped element",
    )
    # Per-ELEMENT non-ASCII boot refusal: one bad element refuses the whole boot (fail-loud).
    assert _raises(
        lambda: _configure(
            scopes="api docs keys",
            role="viewer",
            header_secrets=OLD_SECRET + "," + "delta-" + "u" * 20 + "-\xfc",
        )
    ), "a non-ASCII element in the accept-list must REFUSE the whole boot (per-element)"
    print(
        "  [15b] (b) rotation via LOC_TMS_TRUST_HEADER_SECRETS: 'old,new' honors BOTH "
        "(overlap); an unknown value 401s; two header copies still 401 (M-7); blank elements "
        "dropped; a non-ASCII element refuses at boot (per-element)"
    )

    # ---- [15c] MED-1: the SINGULAR env is a LITERAL value (commas NOT split) ----- #
    # Upgrading an install whose EXISTING LOC_TMS_TRUST_HEADER_SECRET contains a comma must keep
    # authenticating as the WHOLE value -- the single-env comma-split would have broken it into
    # independently-valid weak fragments (proxy break + auth downgrade). RED-PROOF: with the
    # comma-bearing value set as the SINGULAR secret, ONLY the whole literal authenticates; the
    # fragments on either side of the comma are REJECTED.
    COMMA_SECRET = "abc,def-" + "k" * 32  # a real (if unusual) comma-bearing secret
    _configure(scopes="api docs keys", role="viewer", header_secret=COMMA_SECRET)
    _is(
        c.get("/api/projects", headers={HDR: COMMA_SECRET}),
        200,
        "MED-1: a comma-bearing SINGULAR secret authenticates as the WHOLE literal value",
    )
    _is(
        c.get("/api/projects", headers={HDR: "abc"}),
        401,
        "MED-1: a fragment before the comma must NOT authenticate (no split)",
    )
    _is(
        c.get("/api/projects", headers={HDR: "def-" + "k" * 32}),
        401,
        "MED-1: a fragment after the comma must NOT authenticate (no split)",
    )
    # Precedence: when SECRETS (plural) is set, it wins over SECRET (singular).
    _configure(
        scopes="api docs keys",
        role="viewer",
        header_secret="ignored-singular-" + "s" * 20,
        header_secrets=NEW_SECRET,
    )
    _is(c.get("/api/projects", headers={HDR: NEW_SECRET}), 200, "precedence: SECRETS wins")
    _is(
        c.get("/api/projects", headers={HDR: "ignored-singular-" + "s" * 20}),
        401,
        "precedence: the singular value is IGNORED when SECRETS is set",
    )
    print(
        "  [15c] MED-1: the SINGULAR LOC_TMS_TRUST_HEADER_SECRET is a LITERAL (a comma-bearing "
        "value authenticates whole; fragments 401); SECRETS takes precedence when set"
    )

    # ---- [15d] MED-2: a configured-but-empty accept-list FAILS CLOSED at boot ---- #
    # An all-blank LOC_TMS_TRUST_HEADER_SECRETS (","/",,,"/" , ") yields zero elements; it must
    # NOT silently disarm the (b) gate -> the boot is REFUSED (TrustConfigError).
    for blank in (",", ",,,", " , "):
        assert _raises(
            lambda b=blank: _configure(scopes="api docs keys", role="viewer", header_secrets=b)
        ), f"an all-blank LOC_TMS_TRUST_HEADER_SECRETS={blank!r} must FAIL CLOSED at boot"
    # After such a refused boot the top-of-function reset left trust DISARMED (_TRUST=None), so
    # the tokenless path 401s -- it never silently 200s as if the gate had been disarmed.
    _is(
        c.get("/api/projects"),
        401,
        "a refused-boot install serves tokenless 401, never a silent 200",
    )
    print(
        "  [15d] MED-2: a configured-but-empty accept-list (','/',,,'/' , ') REFUSES the boot "
        "(fail-closed), and the failed boot leaves trust disarmed -> tokenless 401"
    )

    # ---- [16] anti-laundering survives the new gate ----------------------------- #
    # A present WRONG token never reaches source_permitted/principal (it is verified in the
    # non-tokenless branch and 401s there), so the new gate does not change it.
    _configure(scopes="api docs keys", role="viewer", header_secret=SECRET)
    _is(
        c.get("/api/projects", headers={"X-Loc-Token": "lsu_bogus", HDR: SECRET}),
        401,
        "a wrong token is still 401 even WITH the correct secret header",
    )
    _is(
        c.get("/api/projects", headers={"X-Loc-Token": "lsu_bogus"}),
        401,
        "a wrong token is still 401 with NO secret header",
    )
    print(
        "  [16] anti-laundering: a present WRONG token still 401s with the secret set "
        "(never reaches source_permitted/principal)"
    )

    # ---- [16b] F5/B-2: source_permitted does NOT consult active() --------------- #
    # B-2 deliberately REMOVED the `if not active(): return True` short-circuit; the first
    # statement is `if not _TRUST_HEADER_SECRET_DIGESTS: return True` (the boot-FROZEN digests,
    # not a live active() read). Pin it BEHAVIORALLY: with trust armed + NO secret, source_permitted
    # returns True even when active() is monkeypatched to RAISE -- proving it never calls
    # active(). A future maintainer who "restores conformance" by re-adding the active() gate
    # (the un-caught mutation F5 flags) blows up here instead of staying green.
    _configure(scopes="api docs keys", role="viewer")  # armed, no secret -> accept-list empty
    _saved_active = trust_auth.active
    trust_auth.active = lambda: (_ for _ in ()).throw(
        AssertionError("source_permitted must not consult active() (B-2)")
    )
    try:
        assert trust_auth.source_permitted(_StubRequest([])) is True, (
            "source_permitted must short-circuit True on an unconfigured secret WITHOUT "
            "consulting active()"
        )
    finally:
        trust_auth.active = _saved_active
    print(
        "  [16b] F5/B-2: source_permitted short-circuits on the boot-frozen secret and "
        "never consults active() (a re-added active() gate would raise here)"
    )

    # ---- [17] (a) HARD-COUPLED to (b) (H-1) ------------------------------------- #
    # Armed + NO secret + X-Loc-Trust-User sent -> (auth=trust) present, NO on-behalf-of.
    _configure(scopes="api docs keys", role="translator")  # no secret
    before = _max_audit_id()
    c.post(
        "/api/projects",
        json={"name": "NoSecretObo", "targets": "fr-FR"},
        headers={USER_HDR: "enduser@client.example"},
    )
    rows = [
        x
        for x in audit.iter_export()
        if _rec_id(x) > before and "POST /api/projects" in x.get("action", "")
    ]
    assert rows and all("(auth=trust)" in x["action"] for x in rows), rows
    assert all("on-behalf-of" not in x.get("detail", "") for x in rows), (
        f"without the (b) secret the forwarded X-Loc-Trust-User must NOT be honored: {rows!r}"
    )
    assert all("on-behalf-of" not in x["action"] for x in rows), rows  # never in action either
    # With the secret armed + correct secret header, the annotation DOES appear (see [19]).
    print(
        "  [17] (a) hard-coupled to (b): armed + NO secret + X-Loc-Trust-User -> "
        "(auth=trust) but NO on-behalf-of marker (annotator returned early)"
    )

    # ---- [18] (a) grants NO authority ------------------------------------------- #
    _configure(scopes="api docs keys", role="viewer", header_secret=SECRET)
    esc = {HDR: SECRET, USER_HDR: "admin@corp"}
    _is(
        c.post("/api/apikeys/issue", json={"scopes": "docs", "name": "x"}, headers=esc),
        403,
        "forwarded admin user must NOT unlock apikeys/issue (still viewer)",
    )
    _is(
        c.post("/api/projects", json={"name": "x", "targets": "fr-FR"}, headers=esc),
        403,
        "forwarded admin user must NOT unlock a write route",
    )
    _is(
        c.post("/api/customers", json={"name": "x"}, headers=esc),
        403,
        "forwarded admin user must NOT unlock a manage route",
    )
    print(
        "  [18] (a) no escalation: a default (viewer) principal + X-Loc-Trust-User="
        "admin@corp is STILL 403 on issue/write/manage -- the header buys zero authority"
    )

    # ---- [19] (a) attribution lands in the audit action (actor unchanged) -------- #
    _configure(scopes="api docs keys", role="translator", header_secret=SECRET)
    before = _max_audit_id()
    c.post(
        "/api/projects",
        json={"name": "OnBehalf", "targets": "fr-FR"},
        headers={HDR: SECRET, USER_HDR: "enduser@client.example"},
    )
    rows = [
        x
        for x in audit.iter_export()
        if _rec_id(x) > before and "POST /api/projects" in x.get("action", "")
    ]
    assert rows, f"no audit row for the on-behalf-of mutation: {rows!r}"
    for x in rows:
        assert x["actor"] == PRINCIPAL, (
            f"the audit ACTOR must stay the trust principal, not the forwarded user: {x!r}"
        )
        assert "(auth=trust)" in x["action"], x
        # HIGH-1: the on-behalf-of attribution lives in the DETAIL column (not the
        # attacker-influenceable action). It is the EXACT detail value.
        assert x.get("detail", "") == "on-behalf-of=enduser@client.example", (
            f"the on-behalf-of attribution must be the audit DETAIL column (HIGH-1): {x!r}"
        )
        assert "on-behalf-of" not in x["action"], (
            f"the on-behalf-of marker must NOT be in the free-form action (HIGH-1): {x!r}"
        )
    assert audit.verify_chain()[0], "the hash chain must verify after the annotated row"
    print(
        f"  [19] (a) attribution: a trust mutation's actor stays {PRINCIPAL!r} while (auth=trust) marks "  # noqa: E501
        "the action and on-behalf-of=<forwarded> lands in the DETAIL column; chain verifies"
    )

    # ---- [19b] HIGH-1: a forged on-behalf-of in the PATH is NOT attributable ----- #
    # `request.url.path` is attacker-controlled + percent-decoded, so a look-alike
    # `on-behalf-of=<victim>` can be smuggled INTO the free-form action via the path. Drive
    # the exact reviewer probe (no headers at all) in the 0b posture (armed, NO secret) --
    # the request even resolves to the trust principal, so the row carries (auth=trust) and
    # looks maximally legitimate. Prove the AUTHORITATIVE attribution (the detail column)
    # stays EMPTY: no colleague can be framed via the URL.
    _configure(scopes="api docs keys", role="viewer")  # armed, NO secret
    before = _max_audit_id()
    c.post(
        "/api/projects%20on-behalf-of=ceo@corp.example", json={"name": "Forged", "targets": "fr-FR"}
    )
    rows = [x for x in audit.iter_export() if _rec_id(x) > before]
    forged = [x for x in rows if "on-behalf-of=ceo@corp.example" in x.get("action", "")]
    assert forged, f"the forged path must echo into the action (sets up the probe): {rows!r}"
    assert all(x.get("detail", "") == "" for x in forged), (
        "HIGH-1: a forged `on-behalf-of=` smuggled via the percent-decoded request PATH must "
        f"NEVER populate the authoritative `detail` attribution column: {forged!r}"
    )
    assert audit.verify_chain()[0], "chain must verify after the forged-path row"
    print(
        "  [19b] HIGH-1: a forged `on-behalf-of=` smuggled via the percent-decoded PATH "
        "echoes only into the free-form action, never the authoritative detail column"
    )

    # ---- [19c] MED-2 (F-1/F-B8): a subject-access export STRUCTURALLY redacts the -- #
    #      forwarded end-user for EVERY shape the annotator accepts -------------------- #
    # The on-behalf-of value lives in the audit `detail` column. `_mask_other_emails` ALONE
    # is NOT enough: the annotator gates on security.validate_email, which deliberately
    # tolerates bare-word logins / intranet addresses / non-ASCII locals / angle-bracket
    # local-parts -- NONE of which gdpr._EMAIL_RE matches -- so those shapes would export RAW
    # (the exact MED-2 disclosure the earlier single-shape arm missed). gdpr.export_user now
    # redacts the WHOLE on-behalf-of token structurally (these rows are always attributed to
    # the trust principal, so the value is by construction a third party). Prove it for all
    # five reviewer-probed shapes: each IS accepted by the annotator (so the raw export is a
    # real leak), and NONE survives the subject-access export.
    _configure(scopes="api docs keys", role="viewer", header_secret=SECRET)
    obo_shapes = [
        "jdoe",
        "bob@internalhost",
        "josé@corp.com",
        "<b>x</b>@x.com",
        "enduser@client.example",
    ]
    stored_obos = []
    for shape in obo_shapes:
        ident = {"kind": "user", "actor": PRINCIPAL, "auth_kind": "trust"}
        trust_auth.annotate_forwarded_actor(ident, _StubRequest([(USER_HDR, shape)]))
        assert "on_behalf_of" in ident, (
            f"the annotator must ACCEPT shape {shape!r} (validate_email tolerates it), so its raw "
            f"export is a REAL leak this arm guards: {ident!r}"
        )
        obo = ident["on_behalf_of"]
        stored_obos.append(obo)
        # Attribute to the principal (as the middleware always does under trust), so
        # gdpr.export_user(principal) -- actor-filtered -- surfaces the row.
        audit.log(
            actor=PRINCIPAL,
            action="POST /api/probe (auth=trust)",
            detail="on-behalf-of=" + obo,
            ip="",
        )
    puid = security.get_by_email(PRINCIPAL).id
    audit_details = [
        rec.get("detail", "") for kind, rec in gdpr.export_user(puid) if kind == "audit"
    ]
    obo_details = [d for d in audit_details if "on-behalf-of=" in d]
    assert obo_details, (
        "the on-behalf-of rows must appear in the principal's subject-"
        f"access audit export: {audit_details!r}"
    )
    # (1) NONE of the raw forwarded values survive anywhere in the export.
    for obo in stored_obos:
        assert all(obo not in d for d in audit_details), (
            f"MED-2 (F-1/F-B8): forwarded value {obo!r} exported RAW in a subject-access copy -- the "  # noqa: E501
            f"on-behalf-of token must be redacted for this shape: {audit_details!r}"
        )
    # (2) Every on-behalf-of token is structurally redacted to [redacted-actor] (the email
    #     mask feeds it: enduser@... -> [redacted-email] -> [redacted-actor]). This also
    #     covers [19]'s and [20]'s stored raw values, now redacted on export.
    assert all(d == "on-behalf-of=[redacted-actor]" for d in obo_details), (
        f"every on-behalf-of detail must be structurally redacted: {obo_details!r}"
    )
    # (3) A detail row with NO on-behalf-of token is byte-UNCHANGED by the new redaction
    #     (the fixed-width lookbehind cannot fire) -- only the pre-existing email mask applies,
    #     so a normal subject-access export is unaffected.
    audit.log(
        actor=PRINCIPAL,
        action="POST /api/other (auth=trust)",
        detail="target=someone@else.example acted",
        ip="",
    )
    plain = [
        rec.get("detail", "")
        for kind, rec in gdpr.export_user(puid)
        if kind == "audit"
        and "on-behalf-of=" not in rec.get("detail", "")
        and "acted" in rec.get("detail", "")
    ]
    assert plain == ["target=[redacted-email] acted"], (
        "a detail row with no on-behalf-of token must be byte-identical to the plain email "
        f"mask -- the redaction lookbehind must NOT fire on it: {plain!r}"
    )
    print(
        "  [19c] MED-2 (F-1/F-B8): a subject-access export STRUCTURALLY redacts the "
        "on-behalf-of token for every annotator-accepted shape (bare word / intranet / "
        "non-ASCII / angle-bracket / real email); a non-obo detail row is byte-unchanged"
    )

    # ---- [20] (a) forwarded-header rejected inputs (H-3 / M-7 / R1-F12) ---------- #
    _configure(scopes="api docs keys", role="translator", header_secret=SECRET)
    # Reserved system actor -> no annotation (end-to-end; "system" is ASCII).
    before = _max_audit_id()
    c.post(
        "/api/projects",
        json={"name": "Reserved", "targets": "fr-FR"},
        headers={HDR: SECRET, USER_HDR: "system"},
    )
    rows = [
        x
        for x in audit.iter_export()
        if _rec_id(x) > before and "POST /api/projects" in x.get("action", "")
    ]
    assert rows and all("on-behalf-of" not in x.get("detail", "") for x in rows), (
        "a reserved X-Loc-Trust-User: system must not annotate (system-actor impersonation): {!r}".format(  # noqa: E501, UP032
            rows
        )
    )
    # CRLF, over-long, multi-copy, blank -> silent drop. Direct annotate calls: deterministic
    # (httpx would either reject or the CRLF must never split the action), gated on the armed
    # secret set just above.
    for label, pairs in (
        ("CRLF", [(USER_HDR, "x@y.com\r\nactor=system")]),
        ("over-long", [(USER_HDR, "a" * 260 + "@x.com")]),
        ("multi-copy", [(USER_HDR, "a@b.com"), (USER_HDR, "c@d.com")]),
        ("blank", [(USER_HDR, "   ")]),
        ("reserved-bare-word", [(USER_HDR, "companion-app")]),
    ):
        ident = {"kind": "user", "actor": PRINCIPAL, "auth_kind": "trust"}
        trust_auth.annotate_forwarded_actor(ident, _StubRequest(pairs))
        assert "on_behalf_of" not in ident, (
            f"a {label} X-Loc-Trust-User must NOT annotate: {ident!r}"
        )
    # An XSS-shaped local part is a VALID email to validate_email (no spaces/control), so it
    # IS annotated -- and stored+rendered escaped (validate_email already barred CRLF/control;
    # Jinja2 autoescape escapes the angle brackets at render; the raw value is inert text).
    before = _max_audit_id()
    xss = "<b>x</b>@x.com"
    c.post(
        "/api/projects",
        json={"name": "Xss", "targets": "fr-FR"},
        headers={HDR: SECRET, USER_HDR: xss},
    )
    rows = [
        x
        for x in audit.iter_export()
        if _rec_id(x) > before and "POST /api/projects" in x.get("action", "")
    ]
    assert rows and all(x.get("detail", "") == ("on-behalf-of=" + xss) for x in rows), rows
    assert all("\r" not in x.get("detail", "") and "\n" not in x.get("detail", "") for x in rows), (
        "no CRLF may appear in the on-behalf-of detail"
    )
    assert audit.verify_chain()[0], "the chain must verify with the XSS-shaped local part stored"
    with Session(engine) as s:
        leaked = s.exec(select(User).where(User.email.like("%enduser@client%"))).first()
    assert leaked is None, "the forwarded end-user must NEVER become a User row"
    print(
        "  [20] (a) rejected inputs: reserved/CRLF/over-long/multi-copy/blank -> NO "
        "annotation; an XSS-shaped local part is stored (chain verifies, escaped at render); "
        "no forwarded user ever becomes a User row"
    )

    # ---- [21] (c) the two warnings (M-8) ---------------------------------------- #
    # (c-boot) NO latch: arming with no secret warns on EACH configure_at_boot call.
    with _capture_trust_warnings() as msgs:
        _configure(scopes="api docs keys", role="viewer")  # no secret
        _configure(scopes="api docs keys", role="viewer")  # no secret again
    boot_warns = [m for m in msgs if "ARMED without LOC_TMS_TRUST_HEADER_SECRET" in m]
    assert len(boot_warns) == 2, f"c-boot warning must fire per call (no latch): {msgs!r}"
    # (c-reject) ONCE-latched: a rejected secret warns once, not per-request.
    _configure(scopes="api docs keys", role="viewer", header_secret=SECRET)  # resets latch
    with _capture_trust_warnings() as msgs:
        c.get("/api/projects")  # missing header -> reject
        c.get("/api/projects", headers={HDR: "nope"})  # wrong header -> reject
    reject_warns = [m for m in msgs if "rejecting a tokenless /api request" in m]
    assert len(reject_warns) == 1, f"c-reject warning must be once-latched: {msgs!r}"
    # (F6) weak-secret nudge: a CONFIGURED secret shorter than 32 chars warns at boot.
    with _capture_trust_warnings() as msgs:
        _configure(scopes="api docs keys", role="viewer", header_secret="short")
    assert any("shorter than 32 characters" in m for m in msgs), (
        "a < 32-char LOC_TMS_TRUST_HEADER_SECRET must fire the weak-secret boot warning: {!r}".format(  # noqa: E501, UP032
            msgs
        )
    )
    # (LOW-8-ii) the dropped-forwarded-value diagnostic: once-latched, ONLY for a PRESENT
    # dropped value (multi-copy / invalid / reserved), never for an absent header.
    # F-B2: probe ABSENT FIRST on the FRESH latch and assert NOTHING has warned yet -- this
    # is what makes the absent!=drop invariant independently falsifiable. If absent ran AFTER
    # a present-drop had already fired the _DROP_WARN throttle, deleting the annotator's
    # `if not values: return` (making absent count as a drop -- the exact D1 property) would
    # be mutation-SURVIVABLE, because the throttle would already be within its window.
    # Ordered this way, deleting that guard reddens the assert below.
    _configure(scopes="api docs keys", role="viewer", header_secret=SECRET)  # resets latch
    with _capture_trust_warnings() as msgs:
        d = {"kind": "user", "actor": PRINCIPAL, "auth_kind": "trust"}
        trust_auth.annotate_forwarded_actor(d, _StubRequest([]))  # ABSENT
        assert [m for m in msgs if "dropped a forwarded X-Loc-Trust-User" in m] == [], (
            "an ABSENT X-Loc-Trust-User must NOT fire the drop diagnostic (absent != drop): {!r}".format(  # noqa: E501, UP032
                msgs
            )
        )
        trust_auth.annotate_forwarded_actor(d, _StubRequest([(USER_HDR, "system")]))  # reserved
        trust_auth.annotate_forwarded_actor(
            d, _StubRequest([(USER_HDR, "a@b.com"), (USER_HDR, "c@d.com")])
        )  # multi-copy
    drop_warns = [m for m in msgs if "dropped a forwarded X-Loc-Trust-User" in m]
    assert len(drop_warns) == 1, (
        f"the drop diagnostic must be once-latched over the two PRESENT drops: {msgs!r}"
    )
    print(
        "  [21] (c) warnings: c-boot fires per configure_at_boot call (no latch, 2/2); "
        "c-reject throttled (once per window); the weak-secret nudge fires (< 32 chars); "
        "the dropped-forwarded-value diagnostic is throttled (present drops only)"
    )

    # ---- [21b] the two per-request diagnostics RE-ARM (not permanent one-shot latches) -- #
    # Both Identity re-gate reviewers flagged the old `_REJECT_WARNED`/`_DROP_WARNED` as
    # PERMANENT per-boot latches: an end-user behind an APPENDING proxy could pre-burn the
    # one-shot diagnostic early and silence the ops signal for the rest of the boot. The
    # coarse monotonic re-arm keeps the flood-safety (at most one line per window) while
    # restoring a live signal — and each re-armed line carries the suppressed-count. Proven
    # here end-to-end with a FAKE monotonic clock swapped onto the module (isolated: only
    # _warn_due reads trust_auth.time), never by poking the throttle state directly.
    import types as _types

    _configure(scopes="api docs keys", role="viewer", header_secret=SECRET)  # resets throttle
    _real_time = trust_auth.time
    _clock = {"t": _real_time.monotonic()}
    trust_auth.time = _types.SimpleNamespace(monotonic=lambda: _clock["t"])
    try:
        with _capture_trust_warnings() as msgs:
            c.get("/api/projects")  # reject #1 -> DUE (first) -> warns
            c.get("/api/projects")  # reject #2 -> same window -> suppressed
            c.get("/api/projects")  # reject #3 -> same window -> suppressed
            within = [m for m in msgs if "rejecting a tokenless" in m]
            assert len(within) == 1, f"still once WITHIN the window: {msgs!r}"
            # advance the monotonic clock past the re-arm window
            _clock["t"] += trust_auth._WARN_REARM_SECONDS + 1.0
            c.get("/api/projects")  # reject #4 -> window elapsed -> re-warns
        rearmed = [m for m in msgs if "rejecting a tokenless" in m]
        assert len(rearmed) == 2, (
            "the c-reject diagnostic MUST re-arm after the window (a permanent latch would "
            f"stay at 1 — the exact reviewer-flagged pre-burn hole): {msgs!r}"
        )
        assert "2 more suppressed" in rearmed[1], (
            f"the re-armed line must carry the count suppressed during the window: {rearmed[1]!r}"
        )
        # The drop diagnostic shares the same throttle law — prove it re-arms too.
        _clock["t"] += trust_auth._WARN_REARM_SECONDS + 1.0
        with _capture_trust_warnings() as msgs:
            d = {"kind": "user", "actor": PRINCIPAL, "auth_kind": "trust"}
            trust_auth.annotate_forwarded_actor(d, _StubRequest([(USER_HDR, "system")]))  # due
            trust_auth.annotate_forwarded_actor(
                d, _StubRequest([(USER_HDR, "system")])
            )  # suppressed
            _clock["t"] += trust_auth._WARN_REARM_SECONDS + 1.0
            trust_auth.annotate_forwarded_actor(d, _StubRequest([(USER_HDR, "system")]))  # re-warns
        drops = [m for m in msgs if "dropped a forwarded X-Loc-Trust-User" in m]
        assert len(drops) == 2, f"the drop diagnostic MUST re-arm after the window: {msgs!r}"
    finally:
        trust_auth.time = _real_time
    print(
        "  [21b] re-arm: the c-reject AND drop diagnostics re-warn after the monotonic "
        "window (no longer permanent one-shot latches), each re-armed line carrying the "
        "suppressed-count; still once WITHIN a window (flood-safe)"
    )

    # ---- [21c] MED-3: the "unconfigured at boot" diagnostic re-arms too ---------- #
    # `_UNCONFIGURED_WARN` (switch ON at request time but _TRUST not resolved at boot) was still
    # a permanent one-shot; route it through _warn_due so a pre-burn can't silence the REAL
    # misconfiguration an operator hits ~11 min later. Drive principal() directly (the fail-
    # closed path), with _TRUST forced None to simulate a switch flipped on AFTER boot.
    _set(embedded_trust_host_auth="1")
    trust_auth._TRUST = None
    trust_auth._UNCONFIGURED_WARN.update(next_at=0.0, suppressed=0)
    _real_time = trust_auth.time
    _clock = {"t": _real_time.monotonic()}
    trust_auth.time = _types.SimpleNamespace(monotonic=lambda: _clock["t"])
    try:
        with _capture_trust_warnings() as msgs:
            assert trust_auth.principal() is None  # pre-burn -> warns (fail-closed)
            assert trust_auth.principal() is None  # same window -> suppressed
            first = [m for m in msgs if "no trust principal was provisioned" in m]
            assert len(first) == 1, f"unconfigured warn once within the window: {msgs!r}"
            _clock["t"] += trust_auth._WARN_REARM_SECONDS + 1.0  # ~11 min later
            assert trust_auth.principal() is None  # real misconfig persists -> re-warns
        warns = [m for m in msgs if "no trust principal was provisioned" in m]
        assert len(warns) == 2, (
            f"the unconfigured-at-boot diagnostic MUST re-arm after the window (MED-3): {warns!r}"
        )
    finally:
        trust_auth.time = _real_time
    _configure(scopes="api docs keys", role="viewer", header_secret=SECRET)  # clean armed state
    print(
        "  [21c] MED-3: the 'switch flipped on after boot' fail-closed diagnostic re-arms "
        "after the window (no longer a pre-burnable one-shot)"
    )

    # ---- [22] byte-neutral: with the secret UNSET every path is 0b -------------- #
    _configure(scopes="api docs keys", role="viewer")  # no secret
    _is(c.get("/api/projects"), 200, "no secret -> 0b tokenless read 200")
    _is(
        c.get("/api/projects", headers={USER_HDR: "enduser@client.example"}),
        200,
        "an unsolicited X-Loc-Trust-User is inert without the (b) secret",
    )
    print(
        "  [22] byte-neutral: with LOC_TMS_TRUST_HEADER_SECRET unset the branch is 0b "
        "exactly -- a stray X-Loc-Trust-User is inert"
    )


def _an_org_id() -> int:
    """The lowest-id org, creating one if the install seeded none."""
    with Session(engine) as s:
        row = s.exec(select(Organization).order_by(Organization.id)).first()
        if row is not None:
            return row.id
        org = Organization(name="Trust Org")
        s.add(org)
        s.commit()
        s.refresh(org)
        return org.id


def _max_audit_id() -> int:
    ids = [_rec_id(rec) for rec in audit.iter_export()]
    return max(ids) if ids else 0


def _rec_id(rec) -> int:
    return rec.get("id", 0) if isinstance(rec, dict) else getattr(rec, "id", 0)


def main() -> None:
    if _NONEMBEDDED_CHILD:
        nonembedded_child_main()
    else:
        embedded_main()


if __name__ == "__main__":
    main()
