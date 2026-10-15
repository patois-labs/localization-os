"""Audit actor attribution: every row names who or what acted (offline).

2026-09-25, from the live demo audit log: three internet-scanner probes
(POST /api/gql, /api/graphql, /api) were written with actor "anonymous". The rule since:
the audit log never says "anonymous". A request that resolved no identity is written as
`unauthenticated <client ip>` (or `unauthenticated (unknown address)`), and its detail
carries `refused <status>`, the user agent (control characters stripped, about 120 chars)
and, when a wrong key or bearer token was sent, only its first few characters.

Proves, end to end through the real middleware stack:
  [1] an unauthenticated POST /api/graphql (and /api/gql, /api) -> actor
      `unauthenticated 203.0.113.7`, detail `refused <the status actually returned>`
  [2] a wrong X-Loc-Token / Bearer token shows only its prefix, never the whole value
  [3] a valid per-user key still attributes to the key holder
  [4] a session user still attributes to their email; a failed sign-in naming a reserved
      actor and a session-less sign-out name the caller, not "anonymous"/"system"
  [5] no determinable client address -> `unauthenticated (unknown address)`
  [6] the pre-auth feature-gate 404 records `refused 404`
  [7] the detail helper strips control characters and bounds the user agent
  [8] the new label is reserved: no account can claim it; org-scoped views never show it
  [9] a legacy "anonymous" row renders as `unauthenticated (legacy row)` on /audit only;
      the stored value, the export and the chain are untouched
  [10] no row anywhere carries actor "anonymous" or a blank actor
  [11] a failed sign-in naming no account (zero-width, fullwidth, NBSP, Cyrillic or injected
       names) is actor `unauthenticated <ip>`; detail keeps only an email, masks the rest
  [12] reserved-name lookalikes are refused at every account-creation seam and invites
  [13] refused vs accepted webhooks, companion-app attribution, a user agent carrying `"`
       and `;`, and a genuine key refused for a suspended org (`suspended-key=`); [13b] the
       same again under audit_fail_open=0 (fail-closed pre-record)
  [14] a password typed into the email box never reaches a row or the export: a name that
       is not an email is `typed-name-masked id=<keyed hex>`; an email is kept
       (quoted, capped at 80 + "...") only when its domain belongs to an account, else
       masked with nothing kept, not even its domain (`Welcome@2024.Fall` is an
       address); an LDAP-verified name is quoted and capped
  [15] ZWNJ / ZWJ pass validate_email only in an IDNA2008 joining context (Persian,
       Devanagari); between Latin letters, at either end, doubled, or inside a reserved
       word they are refused, and U+200B / U+2060 stay refused everywhere
  [16] the boot scan logs one warning naming (by id only) accounts that already carry a
       lookalike or invisible-character email, is silent on a clean DB and survives a DB error

Documented in: docs/architecture/audit-chain.md
"""

import json
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_auditactor_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from starlette.requests import Request

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Organization, Role, User
from app.services import apikeys, audit, security, settings
from tests._csrf import csrf_for, sign_out

PROBE_IP = "203.0.113.7"
PROBE_ACTOR = f"unauthenticated {PROBE_IP}"
ADMIN = "auditor@example.com"
PW = "Correct-Horse-Battery-9!"
LEGACY_ACTION = "POST /api/legacy-probe"
SHORT_GUESS = "abcd"
BAD_PW_RESERVED = "nope-nope-nope"
BAD_PW_REAL = "wrong-password-123"
#: A password typed into the email box by mistake (gate audit-actor round 2, LOW-6).
MISTYPED_PW = "Tr0ub4dor&3xyz"
#: Passwords that are also valid email addresses (gate audit-typedname round 1, LOW-1).
EMAIL_SHAPED_PWS = ("P@ss.word", "Welcome@2024.Fall", "Summer2024@Home.net")


def _client(ip: str = PROBE_IP) -> TestClient:
    return TestClient(app, client=(ip, 50123))


def _newest_id() -> int:
    with Session(engine) as s:
        row = s.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
        return row.id if row else 0


def _rows_since(mark: int) -> list[AuditLog]:
    with Session(engine) as s:
        return list(s.exec(select(AuditLog).where(AuditLog.id > mark).order_by(AuditLog.id)).all())


def _one(mark: int, action: str) -> AuditLog:
    rows = [r for r in _rows_since(mark) if r.action == action]
    assert len(rows) == 1, (action, [(r.actor, r.action, r.detail) for r in _rows_since(mark)])
    return rows[0]


def _raw_request(headers: list[tuple[bytes, bytes]], client=(PROBE_IP, 1)) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/graphql",
            "headers": headers,
            "client": client,
            "session": {},
        }
    )


def test_1_unauthenticated_probes():
    c = _client()
    ua = "Mozilla/5.0 (compatible; ProbeBot/2.1) " + "x" * 300
    for path in ("/api/graphql", "/api/gql", "/api"):
        mark = _newest_id()
        r = c.post(path, json={"query": "{__typename}"}, headers={"User-Agent": ua})
        assert r.status_code >= 400, (path, r.status_code)
        row = _one(mark, f"POST {path}")
        assert row.actor == PROBE_ACTOR, row.actor
        assert row.ip == PROBE_IP, row.ip
        assert row.detail.startswith(f"refused {r.status_code}; user-agent="), row.detail
        assert "ProbeBot/2.1" in row.detail and "x" * 200 not in row.detail, row.detail
        assert "wrong-key-start" not in row.detail, row.detail
        print(f"  [1] POST {path} -> {r.status_code}: {row.actor!r} / {row.detail[:60]!r}...")
    # A real token-guarded route answers 401 and the row says so.
    mark = _newest_id()
    r = c.post("/api/projects", json={"name": "x"})
    assert r.status_code == 401, r.status_code
    row = _one(mark, "POST /api/projects")
    assert row.actor == PROBE_ACTOR and row.detail.startswith("refused 401"), row.detail
    print("  [1] POST /api/projects (real route, no token) -> 401: 'refused 401'")


def test_2_wrong_token_prefix_only():
    c = _client()
    wrong = "lsu_" + "WrongKeyValue" * 4
    mark = _newest_id()
    r = c.post("/api/graphql", json={}, headers={"X-Loc-Token": wrong})
    row = _one(mark, "POST /api/graphql")
    assert row.actor == PROBE_ACTOR, row.actor
    assert f"refused {r.status_code}" in row.detail, row.detail
    assert f"wrong-key-start={wrong[:12]}..." in row.detail, row.detail
    assert wrong not in row.detail and wrong[:13] not in row.detail, row.detail

    bearer = "eyJhbGciOiJIUzI1NiJ9.secret-bearer-body.signature"
    mark = _newest_id()
    c.post("/api/graphql", json={}, headers={"Authorization": f"Bearer {bearer}"})
    row = _one(mark, "POST /api/graphql")
    assert f"wrong-key-start={bearer[:12]}..." in row.detail, row.detail
    assert bearer[:13] not in row.detail, row.detail

    # A short guess is never shown whole: at most half of it.
    mark = _newest_id()
    c.post("/api/graphql", json={}, headers={"X-Loc-Token": SHORT_GUESS})
    row = _one(mark, "POST /api/graphql")
    assert "wrong-key-start=ab..." in row.detail and "abcd" not in row.detail, row.detail
    print("  [2] wrong X-Loc-Token / Bearer / short token: prefix only, never the whole value")


def test_3_valid_key_attributes_to_holder():
    u = security.get_by_email(ADMIN)
    _, key = apikeys.create_key(u.id, "audit-actor", scopes="api")
    mark = _newest_id()
    r = _client().post("/api/graphql", json={}, headers={"X-Loc-Token": key})
    row = _one(mark, "POST /api/graphql")
    assert row.actor == ADMIN, row.actor
    assert "refused" not in row.detail and "wrong-key-start" not in row.detail, row.detail
    print(f"  [3] valid key -> {r.status_code}, actor {row.actor!r}")


def test_4_session_and_signin_rows():
    c = _client()
    csrf_for(c)
    # Failed sign-in naming a reserved synthetic actor: never written as that name.
    mark = _newest_id()
    r = c.post(
        "/auth/login",
        data={"email": "anonymous", "password": BAD_PW_RESERVED, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 401, r.status_code
    failed = _one(mark, "login.failed")
    assert failed.actor == PROBE_ACTOR, failed.actor
    mw = _one(mark, "POST /auth/login")
    assert mw.actor == PROBE_ACTOR and mw.detail.startswith("refused 401"), (mw.actor, mw.detail)

    # A failed sign-in naming a real account keeps naming that account (org-visible).
    mark = _newest_id()
    c.post(
        "/auth/login",
        data={"email": ADMIN, "password": BAD_PW_REAL, "next": "/"},
        follow_redirects=False,
    )
    assert _one(mark, "login.failed").actor == ADMIN
    security.clear_login_failures(ADMIN, PROBE_IP)

    # A real sign-in: the middleware row and every later mutating row name the user.
    mark = _newest_id()
    r = c.post(
        "/auth/login", data={"email": ADMIN, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    assert _one(mark, "login.success").actor == ADMIN
    assert _one(mark, "POST /auth/login").actor == ADMIN
    csrf_for(c)
    mark = _newest_id()
    r = c.post("/auth/keepalive")
    assert r.status_code == 200, r.status_code
    row = _one(mark, "POST /auth/keepalive")
    assert row.actor == ADMIN and "refused" not in row.detail, (row.actor, row.detail)

    # Sign-out with no session names the caller, never "system".
    mark = _newest_id()
    sign_out(_client(), follow_redirects=False)
    assert _one(mark, "logout").actor == PROBE_ACTOR
    print("  [4] session user, failed sign-in (reserved + real name) and bare sign-out attributed")


def test_5_unknown_address():
    settings.set_many({"trusted_proxy_ips": "203.0.113.0/24"})
    try:
        mark = _newest_id()
        _client().post("/api/graphql", json={}, headers={"X-Forwarded-For": "junk"})
        row = _one(mark, "POST /api/graphql")
        assert row.actor == "unauthenticated (unknown address)", row.actor
        assert row.ip == "" and "malformed-x-forwarded-for=" in row.detail, row.detail
    finally:
        settings.set_many({"trusted_proxy_ips": ""})
    print("  [5] no determinable address -> 'unauthenticated (unknown address)'")


def test_6_feature_gate_404():
    settings.set_many({"security_auth_required": "0", "routing_enabled": "0"})
    try:
        mark = _newest_id()
        r = _client().post("/api/routing/rules", json={})
        assert r.status_code == 404, r.status_code
        row = _one(mark, "POST /api/routing/rules")
        assert row.actor == PROBE_ACTOR and row.detail.startswith("refused 404"), (
            row.actor,
            row.detail,
        )
    finally:
        settings.set_many({"security_auth_required": "1", "routing_enabled": "1"})
    print("  [6] pre-auth feature-gate 404 -> 'refused 404'")


def test_7_detail_sanitizes():
    ua = b"Evil\x01Agent\x1b[31m\x7f\x85 " + b"y" * 400
    req = _raw_request([(b"user-agent", ua), (b"x-loc-token", b"lsk_\x01\x85secretvalue-long")])
    detail = security.unauthenticated_detail(req, 401)
    assert detail.startswith('refused 401; user-agent="EvilAgent[31m '), detail
    assert all(c.isprintable() for c in detail), repr(detail)
    quoted = detail.split("user-agent=", 1)[1].split("; wrong-key-start=", 1)[0]
    assert len(quoted) <= 120 + 3 + 2, len(quoted)
    # A value in the shared token's "lsk_" format shows at most 8 characters (gate round 1,
    # LOW-2), the two control bytes masked as "?".
    assert "secretvalue" not in detail and "wrong-key-start=lsk_??se..." in detail, detail
    assert (
        security.unauthenticated_detail(_raw_request([]), 405) == "refused 405; user-agent=(none)"
    )
    assert security.unauthenticated_detail(_raw_request([]), 303).startswith("status 303")
    assert security.unauthenticated_actor(_raw_request([], client=None)) == (
        "unauthenticated (unknown address)"
    )
    print("  [7] detail helper: control chars stripped, UA bounded, token prefix bounded")


def test_8_reserved_and_org_scope():
    for name in (
        "anonymous",
        "unauthenticated",
        "Unauthenticated 203.0.113.7",
        "unauthenticated (unknown address)",
    ):
        assert security.is_reserved_actor(name), name
        try:
            security.create_user(name, "X", Role.TRANSLATOR, PW, enforce_policy=False)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{name!r} should be reserved")
    assert not security.is_reserved_actor("unauthenticated.person@example.com")
    assert not security.is_reserved_actor(ADMIN)

    # Org-scoped audit (customer /audit, GDPR export) matches actor against member emails:
    # an unauthenticated row is never in any org's view, exactly like "anonymous" before.
    with Session(engine) as s:
        org = Organization(name="Audit Actor Org")
        s.add(org)
        s.commit()
        s.refresh(org)
        u = s.exec(select(User).where(User.email == ADMIN)).one()
        u.org_id = org.id
        s.add(u)
        s.commit()
        org_id = org.id
    # #487: rows are confined by the org the actor was in WHEN THEY WERE WRITTEN, so the
    # admin's earlier rows (written before it had an org) are not in this org's view; a row
    # written now is.
    audit.log(actor=ADMIN, action="org.scope.probe", force=True)
    scoped = audit.recent(limit=5000, org_id=org_id)
    assert scoped and all(r.actor == ADMIN for r in scoped), {r.actor for r in scoped}
    assert audit.count(actor=PROBE_ACTOR, org_id=org_id) == 0
    assert audit.count(actor=PROBE_ACTOR) > 0
    exported = [r["actor"] for r in audit.iter_export(actor=ADMIN)]
    assert exported and set(exported) == {ADMIN}
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == ADMIN)).one()
        u.org_id = None
        s.add(u)
        s.commit()
    print("  [8] label reserved; org-scoped + subject views never include unauthenticated rows")


# Lookalike building blocks, spelled with chr() so no escape sequence can be mangled on the
# way into this file: zero-width space, no-break space, braille blank, Hangul filler,
# Cyrillic small a, and a fullwidth-letter spelling.
ZWSP, NBSP, BRAILLE_BLANK, HANGUL_FILLER, CYR_A = (
    chr(0x200B),
    chr(0xA0),
    chr(0x2800),
    chr(0x3164),
    chr(0x430),
)


def _fullwidth(word: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) for c in word)


LOOKALIKES = (
    "anonymous" + ZWSP,
    ZWSP + "anonymous",
    "system" + ZWSP,
    _fullwidth("system"),
    "unauthenticated" + NBSP + "198.51.100.9",
    "unauthenticated" + BRAILLE_BLANK + "198.51.100.9",
    "unauthenticated\n198.51.100.9",
    "anonymous" + HANGUL_FILLER,
    CYR_A + "nonymous",
    "scheduler" + chr(0x034F),
)


def test_11_lookalike_signins():
    """MEDIUM-1: a failed sign-in never turns typed text into an actor. Every attempt that
    names no existing account is `unauthenticated <ip>`, with the typed name sanitized and
    quoted in detail."""
    c = _client()
    csrf_for(c)
    for typed in (*LOOKALIKES, 'x"; ip=1.1.1.1; actor=system', "nobody@example.com"):
        mark = _newest_id()
        r = c.post(
            "/auth/login",
            data={"email": typed, "password": BAD_PW_REAL, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code in (401, 429), (repr(typed), r.status_code)
        row = _one(mark, "login.lockout" if r.status_code == 429 else "login.failed")
        assert row.actor == PROBE_ACTOR, (repr(typed), repr(row.actor))
        # Only an email address is kept readable; every other name is masked (LOW-6).
        kept = typed == "nobody@example.com"
        prefix = 'typed-name="' if kept else "typed-name-masked id="
        assert row.detail.startswith(prefix), (repr(typed), repr(row.detail))
        assert row.detail.isascii() and all(ch.isprintable() for ch in row.detail), repr(row.detail)
        assert ZWSP not in row.detail and "; " not in row.detail, repr(row.detail)
    # NFKC + format-character stripping happen BEFORE the masking decision and the keyed id:
    # the zero-width variant is recorded exactly as plain "anonymous" is.
    mark = _newest_id()
    c.post(
        "/auth/login",
        data={"email": "anonymous" + ZWSP, "password": BAD_PW_REAL, "next": "/"},
        follow_redirects=False,
    )
    rows = [r for r in _rows_since(mark) if r.action in ("login.failed", "login.lockout")]
    plain = security.typed_name_detail("anonymous")
    assert plain.startswith("typed-name-masked id=") and "len=" not in plain, plain
    assert rows and rows[0].detail == plain, repr(rows[0].detail)
    assert "anonymous" not in rows[0].detail, repr(rows[0].detail)
    print(f"  [11] {len(LOOKALIKES) + 2} lookalike / injected sign-in names -> caller, not actor")


def _export_text(c: TestClient) -> str:
    r = c.get("/audit/export")
    assert r.status_code == 200 and "ndjson" in r.headers.get("content-type", ""), r.status_code
    return r.text


def test_14_typed_password_never_recorded():
    """Gate audit-actor round 2, LOW-6: a password typed into the email box by mistake is
    never stored. A name that is not an email address is recorded masked (a keyed id
    only, not even its length), an email address is kept capped and quoted, and an
    LDAP-verified directory name stays readable but capped and quoted in both target and detail."""
    import hashlib
    from unittest.mock import patch

    from app import config
    from app.services import ldap_auth

    # [a] A password-looking value (no "@") never reaches any row, field or the export.
    first = _client("198.51.100.21")
    csrf_for(first)
    second = _client("198.51.100.22")
    csrf_for(second)
    details = []
    for c in (first, second):
        mark = _newest_id()
        r = c.post(
            "/auth/login",
            data={"email": MISTYPED_PW, "password": BAD_PW_REAL, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 401, r.status_code
        row = _one(mark, "login.failed")
        assert row.actor.startswith("unauthenticated "), row.actor
        details.append(row.detail)
    masked = security.typed_name_detail(MISTYPED_PW)
    assert masked == f"typed-name-masked id={masked[-8:]}", masked
    assert all(ch in "0123456789abcdef" for ch in masked[-8:]), masked
    # Stable for the same input (repeated attempts correlate), different for a different one.
    assert details == [masked, masked], details
    assert security.typed_name_detail(MISTYPED_PW + "!") != masked
    assert security.typed_name_detail(MISTYPED_PW[:-1]) != masked
    # Keyed, never a plain hash a dictionary could reverse: another instance secret gives
    # another id, and the id is not a prefix of the unkeyed SHA-256.
    assert masked[-8:] != hashlib.sha256(MISTYPED_PW.encode()).hexdigest()[:8]
    with patch.object(config, "SESSION_SECRET", config.SESSION_SECRET + "-rotated"):
        assert security.typed_name_detail(MISTYPED_PW) != masked
    with Session(engine) as s:
        rows = s.exec(select(AuditLog)).all()
    for r in rows:
        for field in (r.actor, r.action, r.target, r.detail, r.ip):
            assert MISTYPED_PW not in (field or ""), (r.action, field)
    # A bare word that the app's own email seam tolerates (the seeded `admin` style) and an
    # "@" without a dotted domain are masked too: both are plausible passwords.
    for word in ("S3cretWord", "P@ssw0rd"):
        assert security.typed_name_detail(word).startswith("typed-name-masked id="), word

    # [b] A valid email that matches no account is kept, quoted, capped at 80 plus "...".
    long_email = "a" * 90 + "@example.com"
    mark = _newest_id()
    first.post(
        "/auth/login",
        data={"email": long_email, "password": BAD_PW_REAL, "next": "/"},
        follow_redirects=False,
    )
    row = _one(mark, "login.failed")
    assert row.detail == 'typed-name="' + long_email[:80] + '..."', row.detail
    assert security.typed_name_detail("nobody@example.com") == 'typed-name="nobody@example.com"'
    # The domain match ignores case (addresses are stored lowercase).
    assert security.typed_name_detail("Nobody@EXAMPLE.com") == 'typed-name="Nobody@EXAMPLE.com"'

    # [b2] Gate audit-typedname round 1, LOW-1: a password that is also a valid address
    # (only "@" and "." as symbols) is kept ONLY when its domain belongs to an account;
    # otherwise it is masked like any other name and keeps nothing, not even the domain
    # (for `Welcome@2024.Fall` that would be half the password).
    for pw_like in EMAIL_SHAPED_PWS:
        mark = _newest_id()
        first.post(
            "/auth/login",
            data={"email": pw_like, "password": BAD_PW_REAL, "next": "/"},
            follow_redirects=False,
        )
        row = _one(mark, "login.failed")
        want = security.typed_name_detail(pw_like)
        assert row.detail == want == f"typed-name-masked id={want[-8:]}", (pw_like, row.detail)
        local, _, domain = pw_like.partition("@")
        # (Parts under 5 characters, like the "P" of P@ss.word, are too short to search.)
        for part in (p for p in (pw_like, local, domain) if len(p) >= 5):
            assert part.lower() not in row.detail.lower(), (part, row.detail)
    # LIKE wildcards in a domain match nothing but themselves (the email shape already
    # refuses them; the lookup escapes them anyway).
    assert security._known_email_domain("example.com")
    for wild in ("exampl_.com", "%.com", "%"):
        assert not security._known_email_domain(wild), wild
    # A subdomain or a longer domain ending in a known one is a different domain.
    for near in ("x@mail.example.com", "x@notexample.com"):
        assert security.typed_name_detail(near).startswith("typed-name-masked id="), near
    # A lookup failure masks (fail closed), never keeps.
    with patch.object(security, "_engine", lambda: (_ for _ in ()).throw(RuntimeError("db"))):
        assert security.typed_name_detail("nobody@example.com").startswith("typed-name-masked id=")

    # [c] LDAP: a bind that succeeds for a name with no local account. The name is a real
    # directory username, so it stays readable, but target and detail are both quoted+capped.
    directory_name = 'dir"; ip=1.1.1.1; actor=system ' + "z" * 100
    with (
        patch.object(ldap_auth, "enabled", lambda: True),
        patch.object(ldap_auth, "try_login", lambda _u, _p: (None, ldap_auth.NOT_PROVISIONED)),
    ):
        mark = _newest_id()
        r = first.post(
            "/auth/login",
            data={"email": directory_name, "password": BAD_PW_REAL, "next": "/"},
            follow_redirects=False,
        )
    assert r.status_code == 403, r.status_code
    row = _one(mark, "auth.ldap_unprovisioned")
    quoted = security.quoted_field(directory_name, 80)
    assert row.target == quoted and row.detail == "typed-name=" + quoted, (row.target, row.detail)
    assert quoted.startswith('"dir\\"\\u003b ip=') and quoted.endswith('z..."'), quoted
    assert len(quoted) == len(security.quoted_field(directory_name[:80], 80)) + 3, quoted
    assert "; " not in row.target and "z" * 81 not in row.target, row.target

    # [d] The SIEM export (NDJSON) carries none of the password either.
    admin = _client()
    csrf_for(admin)
    r = admin.post(
        "/auth/login", data={"email": ADMIN, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, r.status_code
    exported = _export_text(admin)
    assert masked in exported and MISTYPED_PW not in exported, "export leaks the typed value"
    assert json.dumps(MISTYPED_PW)[1:-1] not in exported
    # LOW-1: an email-shaped password leaves neither its local part nor its domain in any
    # audit row or in the export (checked case-insensitively: the domain would be lowercased).
    with Session(engine) as s:
        all_rows = s.exec(select(AuditLog)).all()
    for pw_like in EMAIL_SHAPED_PWS:
        local, _, domain = pw_like.partition("@")
        for part in (x.lower() for x in (local, domain) if len(x) >= 5):
            assert part not in exported.lower(), ("export", pw_like, part)
            for r in all_rows:
                for field in (r.actor, r.action, r.target, r.detail, r.ip):
                    assert part not in (field or "").lower(), (pw_like, part, r.action, field)
    print("  [14] typed password masked (stable keyed id), email kept + capped, LDAP quoted")


def test_12_lookalike_creation_refused():
    """LOW-1: no account-creation door accepts a reserved name or a lookalike of one."""
    for name in LOOKALIKES:
        assert security.is_reserved_actor(name), repr(name)
        for enforce in (True, False):
            try:
                security.create_user(name, "X", Role.TRANSLATOR, PW, enforce_policy=enforce)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{name!r} (enforce_policy={enforce}) should be refused")
        try:
            security.create_invite(name, Role.TRANSLATOR, created_by=ADMIN)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invite for {name!r} should be refused")
    # A zero-width character in an otherwise ordinary address is refused too, so one real
    # address can never display as another.
    try:
        security.create_user("person" + ZWSP + "@example.com", "X", Role.TRANSLATOR, PW)
    except ValueError:
        pass
    else:
        raise AssertionError("an address carrying a zero-width character must be refused")
    ordinary = "unauthenticated.person@example.com"
    assert not security.is_reserved_actor(ordinary)
    security.create_user(ordinary, "Ordinary", Role.TRANSLATOR, PW)
    assert security.get_by_email(ordinary) is not None
    print("  [12] reserved-name lookalikes refused at create_user (both paths) and invites")


def test_13_webhooks_companion_ua_suspended():
    from unittest.mock import patch

    from app.main import _audit_mutating_request
    from app.web import deps

    # Refused webhook (no connection can sign for it) -> the caller.
    mark = _newest_id()
    r = _client().post("/webhooks/connectors/987654", content=b"{}")
    assert r.status_code >= 400, r.status_code
    row = _one(mark, "POST /webhooks/connectors/987654")
    assert row.actor == PROBE_ACTOR and row.detail.startswith(f"refused {r.status_code}"), (
        row.actor,
        row.detail,
    )
    # Accepted webhook (the provider's verified signature did the work) keeps "system".
    mark = _newest_id()
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/webhooks/connectors/1",
            "headers": [],
            "client": (PROBE_IP, 1),
            "session": {},
        }
    )
    _audit_mutating_request(req, 200)
    row = _one(mark, "POST /webhooks/connectors/1")
    assert row.actor == "system" and "refused" not in row.detail, (row.actor, row.detail)

    # The shared connector token still attributes to companion-app.
    shared = deps.connector_api_token()
    mark = _newest_id()
    _client().post("/api/graphql", json={}, headers={"X-Loc-Token": shared})
    row = _one(mark, "POST /api/graphql")
    assert row.actor == "companion-app" and "refused" not in row.detail, (row.actor, row.detail)

    # LOW-3: a user agent carrying `"` and `;` cannot forge fields for a "; " splitter.
    forged = 'x"; wrong-key-start=lsu_FORGED...; ip=1.1.1.1'
    mark = _newest_id()
    _client().post("/api/graphql", json={}, headers={"User-Agent": forged})
    row = _one(mark, "POST /api/graphql")
    fields = row.detail.split("; ")
    assert len(fields) == 2 and fields[1].startswith('user-agent="'), fields
    assert not any(f.startswith(("wrong-key-start", "ip=")) for f in fields), fields

    # LOW-2: a GENUINE key refused because its org is suspended shows its stored display
    # prefix as suspended-key, never "wrong-key-start" and never more of the secret.
    u = security.get_by_email(ADMIN)
    key_row, key = apikeys.create_key(u.id, "suspended-org", scopes="api")
    with patch.object(deps.access, "user_org_active", lambda _u: False):
        mark = _newest_id()
        _client().post("/api/graphql", json={}, headers={"X-Loc-Token": key})
    row = _one(mark, "POST /api/graphql")
    assert row.actor == PROBE_ACTOR, row.actor
    assert f"suspended-key={key_row.prefix}" in row.detail, row.detail
    assert "wrong-key-start" not in row.detail and key[:13] not in row.detail, row.detail
    # The shared token refused for a suspended bound org: its scheme only.
    with (
        patch.object(deps.access, "multi_org_active", lambda: True),
        patch.object(deps, "_connector_token_org_id", lambda: 1),
        patch.object(deps.access, "org_active", lambda _o: False),
    ):
        mark = _newest_id()
        _client().post("/api/graphql", json={}, headers={"X-Loc-Token": shared})
    row = _one(mark, "POST /api/graphql")
    assert row.actor == PROBE_ACTOR and "suspended-key=lsk_;" not in row.detail, row.detail
    assert row.detail.endswith("suspended-key=lsk_"), row.detail
    assert shared[4:9] not in row.detail, row.detail
    print("  [13] webhooks (refused/accepted), companion-app, forged UA, suspended keys")


def test_13b_same_under_fail_closed():
    """fix-audit-settings gate round 1 M1: fail-closed (the server default since #235)
    writes an identified request's row BEFORE the handler. A session-less request (a
    webhook) is attributed by its outcome, so it must keep the write-after row: a refused
    webhook is still `unauthenticated <ip>` + `refused <status>`, never "system"."""
    settings.set_many({"audit_fail_open": "0"})
    try:
        test_13_webhooks_companion_ua_suspended()
    finally:
        settings.set_many({"audit_fail_open": "1"})
    print("  [13b] the same attribution holds under audit_fail_open=0 (fail-closed)")


# Round 2 (LOW-8): ZERO WIDTH NON-JOINER / JOINER are legitimate inside Persian and Indic
# words (IDNA2008 CONTEXTJ, RFC 5892 A.1/A.2), so they pass only in a joining context.
ZWNJ, ZWJ, WORD_JOINER = chr(0x200C), chr(0x200D), chr(0x2060)
# Persian "mi" + ZWNJ + "khaham" ("I want"): meem, farsi yeh (dual-joining), ZWNJ, khah
# (dual-joining), waw, alef, heh, meem.
PERSIAN_LOCAL = "\u0645\u06cc" + ZWNJ + "\u062e\u0648\u0627\u0647\u0645"
# Devanagari "ksha" written as a half form: ka, virama (U+094D, combining class 9), ZWJ, ssa.
DEVANAGARI_LOCAL = "\u0915\u094d" + ZWJ + "\u0937"
INTL_ACCEPTED = (
    PERSIAN_LOCAL + "@example.ir",
    "user@" + PERSIAN_LOCAL + ".example",  # the same rule applies to the domain
    DEVANAGARI_LOCAL + "@example.in",
    "\u0915\u094d" + ZWNJ + "\u0937@example.in",  # ZWNJ after a virama is allowed too
    # The accented / Greek / CJK probes from the round-2 gate record.
    "Jos\u00e9.Mu\u00f1oz@ejemplo.es",
    "m\u00fcller@b\u00fccher.de",
    "\u7528\u6237@\u4f8b\u5b50.\u5e7f\u544a",
    "\u03b4\u03bf\u03ba\u03b9\u03bc\u03ae@\u03c0\u03b1\u03c1\u03ac\u03b4\u03b5\u03b9\u03b3\u03bc\u03b1.\u03b4\u03bf\u03ba\u03b9\u03bc\u03ae",
)
INTL_REFUSED = (
    "ab" + ZWNJ + "cd@example.com",  # between Latin letters: never a joining context
    "ab" + ZWJ + "cd@example.com",
    ZWNJ + PERSIAN_LOCAL + "@example.ir",  # at the start
    ZWJ + "abcd@example.com",
    PERSIAN_LOCAL + ZWNJ + "@example.ir",  # at the end (before "@")
    "abcd@example.com" + ZWNJ,
    "\u0645\u06cc" + ZWNJ + ZWNJ + "\u062e@example.ir",  # doubled
    "\u0915\u094d" + ZWJ + ZWJ + "\u0937@example.in",
    "\u0627" + ZWNJ + "\u062e@example.ir",  # after alef, which is right-joining only
    "sys" + ZWJ + "tem",
    "system" + ZWNJ,
    ZWJ + "system",
    "ab" + ZWSP + "cd@example.com",  # U+200B and U+2060 stay refused everywhere
    PERSIAN_LOCAL.replace(ZWNJ, ZWSP) + "@example.ir",
    "ab" + WORD_JOINER + "cd@example.com",
    # Fix round 1 (MEDIUM-1): after a virama, a joiner must sit between two letters of the
    # virama's own Indic block. Tamil "tamil" (ta, ma, vowel sign i, llla, pulli U+0BCD)
    # with a joiner before "@", at the end of a domain label, or a borrowed virama on Latin
    # letters (Devanagari virama, Thai phinthu U+0E3A, Khmer coeng U+17D2) are refused.
    "\u0ba4\u0bae\u0bbf\u0bb4\u0bcd" + ZWNJ + "@example.in",
    "\u0ba4\u0bae\u0bbf\u0bb4\u0bcd" + ZWJ + "@example.in",
    "user@\u0915\u094d" + ZWNJ + ".example",
    "user@example.\u0915\u094d" + ZWJ,
    "adm\u094d" + ZWJ + "in@example.com",
    "admin\u0e3a" + ZWJ + "@example.com",
    "admin\u17d2" + ZWJ + "@example.com",
    "\u0915\u094d" + ZWJ + "a@example.in",  # Devanagari virama, Latin letter after
    "\u0915\u094d" + ZWJ + "\u0ba4@example.in",  # Devanagari virama, Tamil letter after
    # LOW-1: default-ignorable combining marks that render as nothing: COMBINING GRAPHEME
    # JOINER, VS16, VS1, VS17, Khmer inherent vowel, Mongolian FVS1, and a Hangul filler.
    "bob\u034f@example.com",
    "bob\ufe0f@example.com",
    "bob\ufe00@example.com",
    "bob\U000e0100@example.com",
    "bob\u17b4@example.com",
    "bob\u180b@example.com",
    "bob\u3164@example.com",
)
# Tamil "tamil" + "nadu" (na, vowel sign aa, tta, vowel sign u). Tamil forms no conjunct
# after the pulli, so a ZWNJ there renders as nothing: the two spellings look identical.
TAMIL = "\u0ba4\u0bae\u0bbf\u0bb4\u0bcd"
TAMILNADU = TAMIL + "\u0ba8\u0bbe\u0b9f\u0bc1"


def test_15_joiners_only_in_joining_context():
    """LOW-8: ZWNJ/ZWJ accepted in a Persian or Indic joining context, refused elsewhere."""
    for addr in INTL_ACCEPTED:
        assert security.validate_email(addr) == addr.lower(), ascii(addr)
    for addr in INTL_REFUSED:
        try:
            security.validate_email(addr)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{addr!a} must be refused")
    # A joiner anywhere inside a reserved word is still reserved (Latin is never a joining
    # context, and the reserved-name skeleton strips it either way).
    for name in ("sys" + ZWJ + "tem", ZWNJ + "system", "anonymous" + ZWJ, "s" + ZWNJ + "cheduler"):
        assert security.is_reserved_actor(name), ascii(name)
        try:
            security.create_user(name, "X", Role.TRANSLATOR, PW)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{name!a} should be refused")
    # End to end: both joining-context addresses become real accounts and invites.
    for addr in (PERSIAN_LOCAL + "@example.ir", DEVANAGARI_LOCAL + "@example.in"):
        security.create_user(addr, "Intl", Role.TRANSLATOR, PW)
        assert security.get_by_email(addr) is not None, ascii(addr)
    security.create_invite("\u0645\u06cc" + ZWNJ + "\u062e@example.ir", Role.VIEWER, ADMIN)
    # Fix round 1 (MEDIUM-1): an invisible twin of an existing account is refused at every
    # creation seam, in both directions (joiner added, or joiner dropped).
    security.create_user(TAMIL + "@example.in", "Tamil", Role.TRANSLATOR, PW)
    security.create_user(TAMILNADU + "@example.in", "Tamil Nadu", Role.TRANSLATOR, PW)
    twin = TAMIL + ZWNJ + TAMILNADU[len(TAMIL) :] + "@example.in"
    assert security.validate_email(twin) == twin, "a same-block joiner is well-formed"
    twins = (
        twin,
        PERSIAN_LOCAL.replace(ZWNJ, "") + "@example.ir",  # joiner dropped
        DEVANAGARI_LOCAL.replace(ZWJ, ZWNJ) + "@example.in",  # joiner swapped
    )
    for addr in twins:
        for create in (
            lambda a: security.create_user(a, "Twin", Role.TRANSLATOR, PW),
            lambda a: security.create_user(a, "Twin", Role.TRANSLATOR, "", auth_source="sso"),
            lambda a: security.create_invite(a, Role.VIEWER, ADMIN),
        ):
            try:
                create(addr)
            except ValueError:
                pass
            else:
                raise AssertionError(f"twin {addr!a} must be refused")
        assert security.lookalike_account_id(addr) is not None, ascii(addr)
        assert security.get_by_email(addr) is None, ascii(addr)
    try:
        security.create_user(twin, "Twin", Role.TRANSLATOR, PW)
    except ValueError as exc:
        assert str(exc) == security.LOOKALIKE_EMAIL_MESSAGE, str(exc)
        assert str(exc).isascii() and "@" not in str(exc), str(exc)
    else:
        raise AssertionError("twin must be refused with the lookalike message")
    # The trust principal seam refuses a twin too.
    from app.services import trust_auth

    try:
        trust_auth._provision_user(twin, Role.VIEWER, None)
    except trust_auth.TrustConfigError as exc:
        assert security.LOOKALIKE_EMAIL_MESSAGE in str(exc), str(exc)
    else:
        raise AssertionError("trust principal twin must be refused")
    # A clearly different address is not a lookalike.
    assert security.lookalike_account_id("bob@example.com") is None
    print(
        f"  [15] joiners: {len(INTL_ACCEPTED)} intl addresses accepted, "
        f"{len(INTL_REFUSED)} out-of-context probes refused"
    )


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def test_16_boot_scan_flags_existing_lookalikes():
    """LOW-7: the boot scan names pre-existing lookalike / invisible-character accounts."""
    from unittest.mock import patch

    log = logging.getLogger("app.services.security")
    cap = _Capture()
    log.addHandler(cap)
    try:
        # Clean DB (every account so far went through the creation gates): silent.
        assert security.warn_lookalike_accounts() == [], "a clean DB must flag nothing"
        assert not cap.records, [r.getMessage() for r in cap.records]
        # Plant two accounts the way an older release (or a direct insert) left them.
        # Fix round 1: an ASCII reserved word (the SQL prefilter's other branch), a Tamil
        # joiner twin of the account [15] created, and an INACTIVE lookalike, which is not
        # flagged (deactivating is the advice, so it must silence the warning, LOW-2).
        planted, inactive = [], []
        with Session(engine) as s:
            for email, active in (
                ("system" + ZWSP, True),
                ("person" + ZWJ + "@example.com", True),
                ("anonymous", True),
                (TAMIL + ZWNJ + TAMILNADU[len(TAMIL) :] + "@example.in", True),
                ("bob\u034f@example.com", False),
            ):
                u = User(email=email, name="Planted", role=Role.VIEWER, active=active)
                s.add(u)
                s.commit()
                s.refresh(u)
                (planted if active else inactive).append(u.id)
        assert sorted(security.warn_lookalike_accounts()) == sorted(planted)
        warnings = [r for r in cap.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in cap.records]
        msg = warnings[0].getMessage()
        assert all(str(i) in msg for i in planted), msg
        assert "example.com" not in msg and ZWSP not in msg and "@" not in msg, msg
        assert msg.isascii() and "deactivate" in msg, msg
        # The joining-context accounts from [15] are legitimate and never flagged.
        intl = security.get_by_email(PERSIAN_LOCAL + "@example.ir")
        assert intl.id not in planted
        # Nothing was changed: the planted rows still exist and are still active.
        with Session(engine) as s:
            assert all(s.get(User, i).active for i in planted)
        # A DB failure is logged and swallowed, never raised into startup.
        cap.records.clear()

        def _boom():
            raise RuntimeError("db down")

        with patch.object(security, "_engine", _boom):
            assert security.warn_lookalike_accounts() == []
        assert any(r.levelno == logging.ERROR for r in cap.records), cap.records
        # Clean up so later tests see only real accounts.
        with Session(engine) as s:
            for i in planted + inactive:
                s.delete(s.get(User, i))
            s.commit()
    finally:
        log.removeHandler(cap)
    print(
        "  [16] boot scan: silent when clean, one id-only warning when planted, survives DB error"
    )


def test_10_no_anonymous_rows():
    with Session(engine) as s:
        bad = s.exec(
            select(AuditLog).where(
                (AuditLog.actor == "anonymous")
                | (AuditLog.actor == "")
                | (AuditLog.actor.is_(None))
            )
        ).all()
    bad = [r for r in bad if r.action != LEGACY_ACTION]
    assert not bad, [(r.actor, r.action) for r in bad]
    print("  [10] no row written with actor 'anonymous' or blank")


def test_9_legacy_row_display():
    # A row exactly as the pre-2026-09-25 middleware wrote it.
    audit.log(actor="anonymous", action=LEGACY_ACTION, ip=PROBE_IP)
    ok, _checked, _bad = audit.verify_chain()[:3]
    assert ok, "chain must verify with the legacy row in it"
    c = _client()
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": ADMIN, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, r.status_code
    page = c.get("/audit", params={"action": "legacy-probe"})
    assert page.status_code == 200, page.status_code
    assert "unauthenticated (legacy row)" in page.text
    assert ">anonymous<" not in page.text
    # Stored bytes and the SIEM export are untouched (a downstream store re-verifies them).
    exported = list(audit.iter_export(action="legacy-probe"))
    assert [r["actor"] for r in exported] == ["anonymous"], exported
    print("  [9] legacy 'anonymous' row: display-only relabel, export + chain untouched")


def main():
    init_db()
    security.create_user(ADMIN, "Auditor", Role.ADMIN, PW)
    settings.set_many({"security_auth_required": "1", "audit_enabled": "1"})
    for test in (
        test_1_unauthenticated_probes,
        test_2_wrong_token_prefix_only,
        test_3_valid_key_attributes_to_holder,
        test_4_session_and_signin_rows,
        test_5_unknown_address,
        test_6_feature_gate_404,
        test_7_detail_sanitizes,
        test_8_reserved_and_org_scope,
        test_11_lookalike_signins,
        test_12_lookalike_creation_refused,
        test_13_webhooks_companion_ua_suspended,
        test_13b_same_under_fail_closed,
        test_14_typed_password_never_recorded,
        test_15_joiners_only_in_joining_context,
        test_16_boot_scan_flags_existing_lookalikes,
        test_10_no_anonymous_rows,
        test_9_legacy_row_display,
    ):
        test()
    ok, checked = audit.verify_chain()[:2]
    assert ok, f"audit chain broken after {checked} rows"
    print("verify_audit_actor_unauthenticated OK")


if __name__ == "__main__":
    main()
