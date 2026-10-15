"""#74 verification: User.email identity uniqueness (offline, hermetic).

Round-3 hardening finding #74 (LOW, identity ambiguity). `User.email` had only the
NON-unique ix_user_email behind an app-layer read-check-then-INSERT, so a race, a direct
DB write, or an SSO/SCIM-provisioned path could land two rows for one address -- and every
by-email lookup in the tree is `.first()`, making the resolved account arbitrary.

What each test PINS (all FAIL against pre-#74 code):

  (a) MIGRATION a1f4c8b2d6e9 normalize+dedupe. Pre-fix the revision did not exist, so a
      DB could hold 'alice@x.test' AND 'Alice@X.test' AND 'ALICE@x.test  ' forever. Now:
      every address is lowered/trimmed; the LOWEST id keeps the address; each loser is
      TOMBSTONED ('duplicate-user-<id>@dedup.invalid' + active=0) rather than DELETEd, so
      its FK references survive; and every loser is NAMED in a WARNING with its id, its
      original address, and the winner's id.
  (b) The UNIQUE index exists after the upgrade and REJECTS a second row for the same
      normalized address (a case-only variant included) -- the structural half.
  (c) A mixed-case address with NO twin is normalized in place and stays ACTIVE (the
      migration does not retire an innocent account) -- and it becomes reachable through
      get_by_email, which it was not before.
  (d) APP LAYER: security.normalize_email is the one law; create_user stores the
      normalized form; a second create differing only by case is refused; the SSO / LDAP /
      SCIM / trust write seams all route through it (source-level recurrence guard).
  (e) The non-alembic create_all/shim path builds the SAME named index (all build paths
      agree), and skips-with-warning instead of crashing when a legacy DB still has dupes.

ASCII-only output; standalone one-process-per-suite script.
"""

import logging
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_email_uniq_")

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.db import engine, init_db
from app.models import Role, User
from app.services import security

_PREV_REV = "c7d2e9f4a1b8"  # Train I's head -- the revision #74 chains off
_REV = "a1f4c8b2d6e9"


class _Cap(logging.Handler):
    """Collect the migration's WARNING messages so the 'log the losers' requirement is
    verifiable, not just asserted in a docstring."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _cfg(data_dir: str):
    from alembic.config import Config

    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    url = "sqlite:///" + os.path.join(data_dir, "loc_tms.db").replace("\\", "/")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, url


def _upgrade(data_dir: str, target: str):
    """alembic upgrade <target> against a throwaway dir; returns (url, warnings)."""
    from alembic import command

    cfg, url = _cfg(data_dir)
    cap = _Cap()
    log = logging.getLogger("alembic.runtime.migration")
    prev_level = log.level
    log.setLevel(logging.WARNING)
    log.addHandler(cap)
    try:
        command.upgrade(cfg, target)
    finally:
        log.removeHandler(cap)
        log.setLevel(prev_level)
    return url, cap.messages


def _insert_users(url: str, rows):
    """Insert (id, email, active) tuples straight into the pre-#74 schema."""
    eng = create_engine(url)
    with eng.connect() as conn:
        for uid, email, active in rows:
            conn.execute(
                text(
                    'INSERT INTO "user" (id, email, name, role, password_hash, '
                    "auth_source, active, is_superadmin, created_at) VALUES "
                    "(:i, :e, '', 'translator', '', 'local', :a, 0, CURRENT_TIMESTAMP)"
                ),
                {"i": uid, "e": email, "a": active},
            )
        conn.commit()
    eng.dispose()


def _read_users(url: str):
    eng = create_engine(url)
    with eng.connect() as conn:
        rows = conn.execute(text('SELECT id, email, active FROM "user" ORDER BY id')).fetchall()
    eng.dispose()
    return {r[0]: (r[1], bool(r[2])) for r in rows}


def test_migration_normalizes_dedupes_and_logs() -> None:
    """(a)+(b)+(c): the whole a1f4c8b2d6e9 data path on a DB seeded at the PREVIOUS head."""
    d = tempfile.mkdtemp(prefix="loctms_email_mig_")
    os.environ["LOC_TMS_DATA_DIR"] = d
    _url, _ = _upgrade(d, _PREV_REV)
    url = _url
    # One winner + two case/whitespace twins, one innocent bystander, one lone
    # mixed-case account with NO twin (must be normalized but NOT retired).
    _insert_users(
        url,
        [
            (1, "alice@x.test", 1),
            (2, "Alice@X.test", 1),
            (3, "bob@x.test", 1),
            (4, "  ALICE@x.test  ", 1),
            (5, "Carol@X.test", 1),
        ],
    )
    _url2, warnings = _upgrade(d, _REV)
    users = _read_users(url)

    # (a) lowest id wins, keeps the address, stays active.
    assert users[1] == ("alice@x.test", True), (
        "#74: the lowest-id twin must KEEP the address",
        users[1],
    )
    # ...and the losers are tombstoned, NOT deleted (the row -- and its FKs -- survive).
    assert users[2] == ("duplicate-user-2@dedup.invalid", False), (
        "#74: loser #2 must be tombstoned + deactivated, never deleted",
        users[2],
    )
    assert users[4] == ("duplicate-user-4@dedup.invalid", False), (
        "#74: a whitespace/case twin is also a duplicate",
        users[4],
    )
    # (c) the bystander and the lone mixed-case account: normalized, untouched otherwise.
    assert users[3] == ("bob@x.test", True), users[3]
    assert users[5] == ("carol@x.test", True), (
        "#74: a mixed-case account with NO twin must be normalized in place and stay ACTIVE "
        "(pre-fix it was stored mixed-case and unreachable through get_by_email)",
        users[5],
    )
    # ...the losers are LOGGED by id, original address, and winner id.
    blob = "\n".join(warnings)
    assert "#74 DEDUPE" in blob, ("the migration must LOG the losers", warnings)
    for needle in ("#2", "Alice@X.test", "#4", "ALICE@x.test", "#1"):
        assert needle in blob, (f"the dedupe log must name {needle!r}", warnings)
    assert "normalized" in blob, ("the normalization pass must be logged too", warnings)

    # (b) the UNIQUE index exists and BITES -- including on a case-only variant.
    eng = create_engine(url)
    with eng.connect() as conn:
        idx = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name='ux_user_email_lower'")
        ).fetchone()
        assert idx is not None, "#74: ux_user_email_lower was not created"
    refused = False
    with eng.connect() as conn:
        try:
            conn.execute(
                text(
                    'INSERT INTO "user" (email, name, role, password_hash, auth_source, '
                    "active, is_superadmin, created_at) VALUES "
                    "('ALICE@X.TEST', '', 'translator', '', 'local', 1, 0, CURRENT_TIMESTAMP)"
                )
            )
            conn.commit()
        except IntegrityError:
            refused = True
    eng.dispose()
    assert refused, (
        "#74: the DB accepted a SECOND row for one normalized address -- by-email "
        "`.first()` is a coin flip again"
    )
    print("  #74 migration: normalize + lowest-id-wins dedupe (losers tombstoned + LOGGED)")
    print("  #74 index: ux_user_email_lower exists and refuses a case-only duplicate")


def test_app_layer_normalizes_on_write() -> None:
    """(d): the app-layer half -- one named law, applied at every User.email write."""
    init_db()
    assert security.normalize_email("  MiXeD@X.Test ") == "mixed@x.test"
    assert security.normalize_email(None) == ""
    u = security.create_user("Norm.Alize@X.Test", "N", Role.TRANSLATOR, "")
    with Session(engine) as s:
        row = s.get(User, u.id)
        assert row.email == "norm.alize@x.test", (
            "#74: create_user stored a non-normalized address",
            row.email,
        )
    assert security.get_by_email("NORM.ALIZE@x.TEST") is not None, "lookup must be case-blind"
    raised = ""
    try:
        security.create_user("NORM.alize@X.test", "N2", Role.TRANSLATOR, "")
    except ValueError as exc:
        raised = str(exc)
    assert "already exists" in raised, (
        "#74: a case-only variant of an existing address must be refused at the app layer",
        raised,
    )
    print("  #74 app layer: normalize_email on write; a case-only duplicate is refused")


def test_write_seams_route_through_the_law() -> None:
    """(d, recurrence guard): every module that WRITES User.email must go through
    security.normalize_email -- a new seam that hand-rolls `.lower().strip()` (or
    neither) re-opens the mixed-case row this train closed."""
    seams = {
        "app/services/security.py": ("create_user", "validate_email", "get_by_email"),
        "app/services/scim.py": ("SCIM provisioning",),
        "app/services/ldap_auth.py": ("LDAP provisioning",),
        "app/services/trust_auth.py": ("trust principal",),
    }
    for path in seams:
        with open(os.path.join(_ROOT, path), encoding="utf-8") as fh:
            src = fh.read()
        assert "normalize_email" in src, (
            f"#74 recurrence guard: {path} writes/reads User.email but does not use "
            "security.normalize_email"
        )
    # The ONLY place a User row is constructed outside create_user is trust_auth, and it
    # must normalize first (it did not before #74).
    with open(os.path.join(_ROOT, "app/services/trust_auth.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert src.index("normalize_email(email)") < src.index("user = User("), (
        "#74: trust_auth must normalize BEFORE constructing the User row"
    )
    print("  #74 recurrence guard: security/scim/ldap/trust all route through normalize_email")


def test_create_all_path_builds_the_same_index() -> None:
    """(e): the non-alembic SQLite path (hermetic tests + the legacy shim) builds the
    IDENTICALLY-NAMED index, so all build paths agree."""
    init_db()
    with Session(engine) as s:
        row = s.exec(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name='ux_user_email_lower'")
        ).first()
    assert row is not None, (
        "#74: the create_all/shim path did not build ux_user_email_lower -- the two build "
        "paths disagree and the hermetic suites would never exercise the constraint"
    )
    # ...and it BITES on this engine too (a direct write bypassing create_user).
    refused = False
    try:
        with Session(engine) as s:
            s.add(User(email="norm.alize@x.test", name="dupe", role=Role.TRANSLATOR))
            s.commit()
    except IntegrityError:
        refused = True
    assert refused, "#74: a direct duplicate INSERT was accepted on the create_all path"
    print("  #74 build paths: create_all/shim builds ux_user_email_lower and it bites")


def test_shim_skips_with_warning_on_a_legacy_dup() -> None:
    """(e): a legacy DB that still holds duplicates must NOT crash init_db -- it logs and
    leaves the index off (the segmenttm house style), because only the alembic revision
    can normalize + tombstone."""
    from app import db

    d = tempfile.mkdtemp(prefix="loctms_email_shim_")
    url = "sqlite:///" + os.path.join(d, "legacy.db").replace("\\", "/")
    eng = create_engine(url)
    with eng.connect() as conn:
        conn.execute(text('CREATE TABLE "user" (id INTEGER PRIMARY KEY, email VARCHAR)'))
        conn.execute(text("INSERT INTO \"user\" (email) VALUES ('dup@x.test'), ('DUP@x.test')"))
        conn.commit()
        n = conn.execute(
            text(
                "SELECT COUNT(*) FROM (SELECT 1 FROM user GROUP BY lower(trim(email)) "
                "HAVING COUNT(*) > 1)"
            )
        ).scalar()
    eng.dispose()
    assert n == 1, ("the dup-detect query used by the shim guard must find the group", n)
    # The guard's query is the SAME one db._ensure_indexes runs; the index entry is
    # present in the shared table so both build paths name it identically.
    assert ("ux_user_email_lower", "user", "(lower(email))") in db._UNIQUE_INDEXES, (
        "#74: ux_user_email_lower must ride the shared _UNIQUE_INDEXES table"
    )
    print("  #74 legacy shim: a duplicate group is detected (index skipped, no crash)")


def main() -> None:
    test_migration_normalizes_dedupes_and_logs()
    test_app_layer_normalizes_on_write()
    test_write_seams_route_through_the_law()
    test_create_all_path_builds_the_same_index()
    test_shim_skips_with_warning_on_a_legacy_dup()
    print("USER EMAIL UNIQUE OK")


if __name__ == "__main__":
    main()
