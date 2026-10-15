"""CLI settings writes (#529): prompt, confirm and validate-before-write.

  1. `user create` with no --password on a terminal prompts (confirmed, never echoed) and the
     account really signs in with it; a blank answer is the explicit "no local login"; a
     mismatch is a usage error; off a terminal the account is created without a login and a
     warning says so.
  2. The admin verbs that write security-sensitive keys (`admin retention config`, `admin ldap
     config --enabled`, `admin oidc config --enabled`) confirm like `config set`: refused
     without --yes off a terminal (nothing written), applied with --yes.
  3. `admin ldap config` / `oidc config` validate before writing: a refused batch leaves the
     bind password / client secret untouched.

Final line: "CLI SETTINGS WRITES OK".
"""

import contextlib
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cli_setwrites_")

from app.db import init_db
from tests._cli_runner import run_cli

_PW = "Zx9-correct-horse-battery"
_BIND = "ldap-bind-" + "marker-1"
_SECRET_MARK = "oidc-" + "marker-2"


class _TTY(io.StringIO):
    def isatty(self):
        return True


@contextlib.contextmanager
def _terminal(answers):
    """Pretend stdin is a terminal and feed getpass the given answers in order."""
    import getpass

    real_in, real_gp = sys.stdin, getpass.getpass
    it = iter(answers)
    sys.stdin = _TTY()
    getpass.getpass = lambda prompt="": next(it)
    try:
        yield
    finally:
        sys.stdin, getpass.getpass = real_in, real_gp


def test_user_create_prompts():
    from app.kernel import security

    with _terminal([_PW, _PW]):
        code, out, err = run_cli(
            ["user", "create", "--email", "prompted@x.com", "--name", "P", "--role", "translator"]
        )
    assert code == 0, (code, out, err)
    assert security.authenticate("prompted@x.com", _PW), "the prompted password must sign in"
    # blank answer = no local login
    with _terminal([""]):
        code, out, err = run_cli(
            ["user", "create", "--email", "blank@x.com", "--name", "B", "--role", "translator"]
        )
    assert code == 0, (code, out, err)
    assert not security.get_by_email("blank@x.com").password_hash
    # mismatch is a usage error and creates nothing
    with _terminal([_PW, _PW + "x"]):
        code, out, err = run_cli(
            ["user", "create", "--email", "mismatch@x.com", "--name", "M", "--role", "translator"]
        )
    assert code == 2, (code, out, err)
    assert security.get_by_email("mismatch@x.com") is None
    # no terminal, no --password: created without a login, with a warning
    code, out, err = run_cli(
        ["user", "create", "--email", "headless@x.com", "--name", "H", "--role", "translator"]
    )
    assert code == 0 and "no local login" in err, (code, out, err)
    print(
        "  1. user create prompts (confirmed), blank = no login, mismatch = exit 2, headless warns"
    )


def test_user_create_password_stdin():
    """--password-stdin (gate, orchestrator decision): one line from stdin, never echoed,
    never in --json or the audit log; empty line and --password together are usage errors."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.kernel import security
    from app.models import AuditLog

    real_in = sys.stdin
    try:
        sys.stdin = io.StringIO(_PW + "\r\nsecond-line-ignored\n")
        code, out, err = run_cli(
            [
                "--json", "user", "create", "--email", "piped@x.com", "--name", "S",
                "--role", "translator", "--password-stdin",
            ]
        )  # fmt: skip
        assert code == 0, (code, out, err)
        assert _PW not in out and _PW not in err, "password echoed"
        assert "no local login" not in err, (out, err)
        assert security.authenticate("piped@x.com", _PW), "piped password must sign in"
        with Session(engine) as s:
            rows = s.exec(select(AuditLog)).all()
            assert not any(_PW in repr(vars(r)) for r in rows), "password in the audit log"
        # empty line: usage error, nothing created
        sys.stdin = io.StringIO("\n")
        code, out, err = run_cli(
            ["user", "create", "--email", "emptypipe@x.com", "--role", "translator",
             "--password-stdin"]
        )  # fmt: skip
        assert code == 2, (code, out, err)
        assert security.get_by_email("emptypipe@x.com") is None
        # both flags: usage error, nothing created
        sys.stdin = io.StringIO(_PW + "\n")
        code, out, err = run_cli(
            ["user", "create", "--email", "both@x.com", "--role", "translator",
             "--password-stdin", "--password", _PW]
        )  # fmt: skip
        assert code == 2, (code, out, err)
        assert security.get_by_email("both@x.com") is None
    finally:
        sys.stdin = real_in
    print("  1b. user create --password-stdin: signs in, never echoed/audited, empty/both = exit 2")


def test_admin_verbs_confirm_sensitive_keys():
    from app.kernel import settings

    # retention: arms the purge daemon, so it must confirm
    code, out, err = run_cli(["admin", "retention", "config", "--enabled", "on", "--days", "1"])
    assert code == 1, (code, out, err)
    assert settings.get("retention_enabled") != "1", "retention armed without a confirm"
    code, out, err = run_cli(
        ["admin", "retention", "config", "--enabled", "on", "--days", "1", "--yes"]
    )
    assert code == 0, (code, out, err)
    assert settings.get("retention_enabled") == "1"
    code, out, err = run_cli(["admin", "retention", "config", "--enabled", "off", "--yes"])
    assert code == 0, (code, out, err)
    # non-sensitive field of the same verb still needs no confirm? (retention_days is sensitive)
    # ldap / oidc enable
    for verb, key in (("ldap", "ldap_enabled"), ("oidc", "sso_enabled")):
        before = settings.get(key)
        code, out, err = run_cli(["admin", verb, "config", "--enabled", "off"])
        assert code == 1, (verb, code, out, err)
        assert settings.get(key) == before
        code, out, err = run_cli(["admin", verb, "config", "--enabled", "off", "--yes"])
        assert code == 0, (verb, code, out, err)
    # every ldap_* / sso_* key is on SENSITIVE_KEYS, so a plain field needs --yes too
    code, out, err = run_cli(["admin", "ldap", "config", "--base-dn", "dc=example,dc=com"])
    assert code == 1, (code, out, err)
    code, out, err = run_cli(["admin", "ldap", "config", "--base-dn", "dc=example,dc=com", "--yes"])
    assert code == 0, (code, out, err)
    print("  2. retention/ldap/oidc config: refused without --yes, applied with it")


def test_refused_batch_leaves_secrets_untouched():
    from app.kernel import settings

    before_ldap = settings.get("ldap_bind_password_enc")
    code, out, err = run_cli(
        ["admin", "ldap", "config", "--host", "a\x01b", "--bind-password", _BIND]
    )
    assert code != 0, (code, out, err)
    assert settings.get("ldap_bind_password_enc") == before_ldap, (
        "bind secret written by a refused batch"
    )
    before_sso = settings.get("sso_client_secret")
    code, out, err = run_cli(
        ["admin", "oidc", "config", "--issuer", "ftp://nope\x01", "--client-secret", _SECRET_MARK]
    )
    # unconfirmed (no terminal, no --yes): refused, secret untouched
    assert code != 0, (code, out, err)
    assert settings.get("sso_client_secret") == before_sso
    # Gate: CONFIRMED (--yes) but set_many refuses the batch (e.g. arming LDAP without the
    # ldap3 extra): the secret must still be untouched, since it is written only after
    # set_many accepted the rest (#529; it used to be written first, with no audit row).
    real_set_many = settings.set_many

    def _refuse(items, *a, **k):
        # set_secret writes through set_many too: let the secret rows through, so the pin
        # sees the secret land if the verb writes it before (or without) the accepted batch.
        if set(items) <= {"ldap_bind_password_enc", "sso_client_secret"}:
            return real_set_many(items, *a, **k)
        raise RuntimeError("refused by the stub")

    settings.set_many = _refuse
    try:
        code, out, err = run_cli(
            ["admin", "ldap", "config", "--base-dn", "dc=x", "--bind-password", _BIND, "--yes"]
        )
        assert code == 1 and "refused by the stub" in err, (code, out, err)
        assert settings.get("ldap_bind_password_enc") == before_ldap, (
            "bind secret written although set_many refused the batch"
        )
        code, out, err = run_cli(
            ["admin", "oidc", "config", "--client-id", "cid", "--client-secret", _SECRET_MARK,
             "--yes"]
        )  # fmt: skip
        assert code == 1 and "refused by the stub" in err, (code, out, err)
        assert settings.get("sso_client_secret") == before_sso, (
            "client secret written although set_many refused the batch"
        )
    finally:
        settings.set_many = real_set_many
    print("  3. refused ldap/oidc batch (unconfirmed or set_many refusal) leaves secrets untouched")


def main_() -> None:
    init_db()
    # On Windows the NUL device reports isatty() True: pin a genuinely non-terminal stdin.
    sys.stdin = io.StringIO()
    test_user_create_prompts()
    test_user_create_password_stdin()
    test_admin_verbs_confirm_sensitive_keys()
    test_refused_batch_leaves_secrets_untouched()
    print("CLI SETTINGS WRITES OK")


if __name__ == "__main__":
    main_()
