"""#400: concurrent removals share one database transaction guard.

SQLite runs offline. Set LOC_TMS_ADMIN_RACE_POSTGRES_URL explicitly to run the
same matrix in a disposable PostgreSQL schema (never against existing tables).
"""

import itertools
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from tests._bootstrap import init_env

init_env("loctms_admin_race_")
from sqlalchemy import text, update
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import engine, init_db
from app.models import ApiKey, Organization, Role, User
from app.services import access, apikeys, audit, gdpr_common, password_policy, scim, security
from app.services import security_client_ip_one_authority_ as security_impl


def race(db, left, right, mode):
    with Session(db) as s:
        s.exec(update(User).values(active=False))
        source = Organization(name="Source")
        destination = Organization(name="Destination")
        s.add(source)
        s.add(destination)
        s.commit()
        source_id, dest_id = source.id, destination.id
        org_id = None if mode == "orgless" else source_id
        users = [
            User(email=f"{uuid.uuid4().hex}@example.com", role=Role.ADMIN, org_id=org_id)
            for _ in range(2)
        ]
        s.add_all(users)
        s.commit()
        ids = [u.id for u in users]
        emails = [u.email for u in users]
        for uid in ids:
            s.add(ApiKey(user_id=uid, key_hash=uuid.uuid4().hex))
        s.commit()

    first_checked = threading.Event()
    second_checked = threading.Event()
    original = security_impl.is_last_admin_of_own_org

    def pause_after_check(uid, answer):
        if uid == ids[0]:
            first_checked.set()
            # A correct lock prevents the second predicate until the first commits.
            # The old implementation lets both real predicates read two admins.
            second_checked.wait(0.5)
        else:
            second_checked.set()
        return answer

    def checked(session, uid):
        return pause_after_check(uid, original(session, uid))

    original_scoped = security.is_last_active_admin

    def checked_scoped(session, uid, scope):
        return pause_after_check(uid, original_scoped(session, uid, scope))

    def remove(index, operation):
        if index:
            assert first_checked.wait(10), "first request never reached the guard"
        uid = ids[index]
        if operation == "erase":
            return gdpr_common.erase_user(uid, actor=emails[1 - index])[0]
        if operation in ("scim", "scim_delete"):
            try:
                if operation == "scim":
                    scim._apply_user_fields(uid, org_id, emails[1 - index], active=False)
                else:
                    scim.deactivate_user(org_id, emails[1 - index], uid)
                return True
            except scim.ScimError as exc:
                assert exc.status == 409
                return False
        with Session(db) as s:
            # Exercise callers that already loaded the subject into the identity map.
            s.get(User, uid)
            if operation == "role":
                return security.set_role(s, uid, Role.VIEWER, org_id=access.UNSCOPED)["ok"]
            if operation == "active":
                return security.set_active(s, uid, False, org_id=access.UNSCOPED)["ok"]
            try:
                access.move_user_to_org(s, uid, dest_id)
                return True
            except ValueError as exc:
                assert "last active administrator" in str(exc)
                return False

    with (
        patch.object(access, "multi_org_active", return_value=mode != "global"),
        patch.object(gdpr_common, "engine", db),
        patch.object(apikeys, "engine", db),
        patch.object(scim, "engine", db),
        patch.object(audit, "log"),
        patch.object(security_impl, "is_last_admin_of_own_org", checked),
        patch.object(security, "is_last_admin_of_own_org", checked),
        patch.object(security, "is_last_active_admin", checked_scoped),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        a = pool.submit(remove, 0, left)
        b = pool.submit(remove, 1, right)
        results = [a.result(timeout=20), b.result(timeout=20)]
    with Session(db) as s:
        remaining = s.exec(
            select(User).where(User.id.in_(ids), User.active == True, User.role == Role.ADMIN)  # noqa: E712
        ).all()
        if mode != "global":
            remaining = [u for u in remaining if u.org_id == org_id]
        assert results == [True, False], (left, right, mode, results)
        assert len(remaining) == 1, (left, right, mode, remaining)
        survivor = s.get(User, ids[1])
        assert survivor.email == emails[1] and survivor.active and survivor.role == Role.ADMIN
        assert survivor.org_id == org_id
        assert s.exec(select(ApiKey).where(ApiKey.user_id == ids[1])).one().active
        if left == "erase":
            assert not s.exec(select(ApiKey).where(ApiKey.user_id == ids[0])).one().active


def matrix(db):
    failures = []
    for mode in ("global", "org", "orgless"):
        operations = ("erase", "role", "active", "scim") + (() if mode == "global" else ("move",))
        if mode == "org":
            operations += ("scim_delete",)
        for left, right in itertools.combinations_with_replacement(operations, 2):
            try:
                race(db, left, right, mode)
            except Exception as exc:
                failures.append(f"{mode}/{left}/{right}: {exc!r}")
    assert not failures, "\n".join(failures)


def stale_subject(db):
    """A caller's cached non-admin may now be the sole admin of a different org."""
    for operation in ("role", "active", "move"):
        with Session(db) as s:
            org = Organization(name="Stale target")
            other = Organization(name="Old target")
            s.add_all([org, other])
            s.commit()
            oid, other_id = org.id, other.id
            user = User(email=f"{uuid.uuid4().hex}@example.com", org_id=other_id)
            s.add(user)
            s.commit()
            uid = user.id
        with Session(db) as stale, patch.object(access, "multi_org_active", return_value=True):
            cached = stale.get(User, uid)
            with Session(db) as s:
                current = s.get(User, uid)
                current.org_id = oid
                current.role = Role.ADMIN
                s.add(current)
                s.commit()
            assert cached.role != Role.ADMIN
            if operation == "move":
                try:
                    access.move_user_to_org(stale, uid, other_id)
                except ValueError as exc:
                    assert "last active administrator" in str(exc)
                else:
                    raise AssertionError("moved a cached last admin")
            else:
                fn, value = (
                    (security.set_role, Role.VIEWER)
                    if operation == "role"
                    else (security.set_active, False)
                )
                result = fn(stale, uid, value, org_id=access.UNSCOPED)
                assert not result["ok"] and result["reason"] == "last_admin"


def erase_rollback(db):
    with Session(db) as s:
        user = User(email=f"{uuid.uuid4().hex}@example.com")
        s.add(user)
        s.commit()
        uid, email = user.id, user.email
        s.add(ApiKey(user_id=uid, key_hash=uuid.uuid4().hex))
        s.commit()
    with (
        patch.object(gdpr_common, "engine", db),
        patch.object(apikeys, "engine", db),
        patch.object(password_policy, "delete_history", side_effect=RuntimeError("scrub failed")),
    ):
        try:
            gdpr_common.erase_user(uid, actor="operator")
        except RuntimeError as exc:
            assert str(exc) == "scrub failed"
        else:
            raise AssertionError("injected erasure failure did not propagate")
    with Session(db) as s:
        user = s.get(User, uid)
        assert user.active and user.email == email
        assert s.exec(select(ApiKey).where(ApiKey.user_id == uid)).one().active


def scim_creation_race(db):
    """SCIM's second commit must not deactivate a newly-created last admin."""
    with Session(db) as s:
        org = Organization(name="SCIM creation")
        s.add(org)
        s.commit()
        oid = org.id
        old = User(email=f"{uuid.uuid4().hex}@example.com", org_id=oid, role=Role.ADMIN)
        s.add(old)
        s.commit()
        old_id = old.id
    original = security.create_user
    email = f"{uuid.uuid4().hex}@example.com"

    def create_then_erase(*args, **kwargs):
        created = original(*args, **kwargs)
        assert gdpr_common.erase_user(old_id, actor=email, caller_org_id=oid)[0]
        return created

    with (
        patch.object(access, "multi_org_active", return_value=True),
        patch.object(gdpr_common, "engine", db),
        patch.object(apikeys, "engine", db),
        patch.object(scim, "engine", db),
        patch.object(security_impl, "engine", db),
        patch.object(security, "engine", db),
        patch.object(audit, "log"),
        patch.object(scim, "_default_role", return_value=Role.ADMIN),
        patch.object(security, "create_user", side_effect=create_then_erase),
    ):
        try:
            scim.create_user(oid, "operator", {"userName": email, "active": False})
        except scim.ScimError as exc:
            assert exc.status == 409
        else:
            raise AssertionError("SCIM creation deactivated the last admin")
    with Session(db) as s:
        assert s.exec(select(User).where(User.email == email)).one().active


def refusal_releases_writer(db):
    """A refusal must not hold up subsequent audit/key writes in another Session."""
    with Session(db) as s:
        org = Organization(name="Refused mutation")
        s.add(org)
        s.commit()
        oid = org.id
        user = User(email=f"{uuid.uuid4().hex}@example.com", org_id=oid, role=Role.ADMIN)
        s.add(user)
        s.commit()
        uid = user.id
    with patch.object(access, "multi_org_active", return_value=True):
        for fn, value in ((security.set_role, Role.VIEWER), (security.set_active, False)):
            with Session(db) as s:
                s.add(ApiKey(user_id=uid, key_hash=uuid.uuid4().hex))
                s.commit()
            with Session(db) as held, patch.object(apikeys, "engine", db):
                result = fn(held, uid, value, org_id=oid)
                assert result["reason"] == "last_admin"
                assert apikeys.revoke_all_for_user(uid, org_id=oid) == 1


def verify_backend(db):
    failures = []
    for check in (
        matrix,
        stale_subject,
        erase_rollback,
        scim_creation_race,
        refusal_releases_writer,
    ):
        try:
            check(db)
        except Exception as exc:
            failures.append(f"{check.__name__}: {exc!r}")
    assert not failures, "\n".join(failures)


def main():
    init_db()
    verify_backend(engine)
    url = os.environ.get("LOC_TMS_ADMIN_RACE_POSTGRES_URL")
    if url:
        schema = "admin_race_" + uuid.uuid4().hex
        root_engine = create_engine(url)
        with root_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        pg = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        try:
            SQLModel.metadata.create_all(pg)
            verify_backend(pg)
        finally:
            pg.dispose()
            with root_engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            root_engine.dispose()
    else:
        print("PostgreSQL concurrency matrix SKIPPED: no explicit disposable database URL")
    print("verify_admin_mutation_race OK")


if __name__ == "__main__":
    main()
