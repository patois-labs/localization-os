"""GitHub #520 follow-ups on migration a1f4c8b2d6e9 (User.email dedupe), run on SQLite.

verify_user_email_unique pins the base behaviour (normalize, lowest id wins among equals,
losers tombstoned and logged, the unique index bites). This suite pins the two rules added
after it:

  winner   an ACTIVE account that has signed in outranks an inactive or never-used twin, even
           when the twin has the lower id. With no evidence either way the lowest id still wins.
  empty    accounts with NO address are not twins of one another: none is retired, they all
           stay active, and every empty address after the first becomes a unique placeholder
           so the unique index can be built.
  keys     a retired loser's API keys are revoked (a later hand reinstatement revives none);
           the winner's and a placeholdered account's keys are untouched.
  repeat   the migration is idempotent: downgrade (drops only the index) then upgrade again
           changes nothing.

Final line: "USER EMAIL DEDUPE RULES OK".
"""

import logging
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_email_rules_")

from sqlalchemy import create_engine, text

_PREV_REV = "c7d2e9f4a1b8"
_REV = "a1f4c8b2d6e9"
FAILS = []


def check(cond, label):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _cfg(data_dir):
    from alembic.config import Config

    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    url = "sqlite:///" + os.path.join(data_dir, "loc_tms.db").replace("\\", "/")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, url


def _run(data_dir, verb, target):
    from alembic import command

    cfg, url = _cfg(data_dir)
    cap = _Cap()
    log = logging.getLogger("alembic.runtime.migration")
    prev = log.level
    log.setLevel(logging.WARNING)
    log.addHandler(cap)
    try:
        getattr(command, verb)(cfg, target)
    finally:
        log.removeHandler(cap)
        log.setLevel(prev)
    return url, cap.messages


def _insert(url, rows):
    eng = create_engine(url)
    with eng.connect() as conn:
        for uid, email, active, last_login in rows:
            conn.execute(
                text(
                    'INSERT INTO "user" (id, email, name, role, password_hash, auth_source, '
                    "active, is_superadmin, created_at, last_login) VALUES "
                    "(:i, :e, '', 'translator', '', 'local', :a, 0, CURRENT_TIMESTAMP, :l)"
                ),
                {"i": uid, "e": email, "a": active, "l": last_login},
            )
        conn.commit()
    eng.dispose()


def _read(url):
    eng = create_engine(url)
    with eng.connect() as conn:
        rows = conn.execute(text('SELECT id, email, active FROM "user" ORDER BY id')).fetchall()
    eng.dispose()
    return {r[0]: (r[1], bool(r[2])) for r in rows}


d = tempfile.mkdtemp(prefix="loctms_email_rules_")
os.environ["LOC_TMS_DATA_DIR"] = d
url, _ = _run(d, "upgrade", _PREV_REV)
_insert(
    url,
    [
        # group A: the lower id is a dead mixed-case row, the lowercase twin is the live one.
        (1, "Alice@X.test", 1, None),
        (2, "alice@x.test", 1, "2026-09-30 10:00:00.000000"),
        # group B: both signed in; the most recent login keeps the address.
        (3, "bob@x.test", 1, "2026-08-01 10:00:00.000000"),
        (4, "BOB@x.test", 1, "2026-09-01 10:00:00.000000"),
        # group C: the lower id is deactivated, the higher is active.
        (5, "carol@x.test", 0, "2026-09-30 10:00:00.000000"),
        (6, "Carol@x.test", 1, None),
        # group D: no evidence either side -> lowest id wins (the old rule).
        (7, "dave@x.test", 1, None),
        (8, "DAVE@x.test", 1, None),
        # empty addresses: three accounts, never twins.
        (9, "", 1, None),
        (10, "  ", 1, None),
        (11, "", 1, None),
    ],
)
# API keys: one on a loser (#1), one on its winner (#2), one on a placeholdered account (#10).
_eng = create_engine(url)
with _eng.connect() as conn:
    for kid, uid in ((1, 1), (2, 2), (3, 10)):
        conn.execute(
            text(
                "INSERT INTO apikey (id, user_id, name, prefix, key_hash, scopes, active, "
                "created_at) VALUES (:k, :u, 'k', 'lsu_', :h, 'api', 1, CURRENT_TIMESTAMP)"
            ),
            {"k": kid, "u": uid, "h": f"hash-{kid}"},
        )
    conn.commit()
_eng.dispose()
_, warnings = _run(d, "upgrade", _REV)
users = _read(url)


def _keys(url):
    eng = create_engine(url)
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT user_id, active FROM apikey ORDER BY id")).fetchall()
    eng.dispose()
    return {r[0]: bool(r[1]) for r in rows}


keys = _keys(url)
check(keys[1] is False, f"a retired loser's API key is revoked (#422 posture) {keys}")
check(keys[2] is True, f"the winner's API key is untouched {keys}")
check(keys[10] is True, f"a placeholdered (active) account keeps its API key {keys}")

check(
    users[2] == ("alice@x.test", True), f"A: the live lowercase twin keeps the address {users[2]}"
)
check(
    users[1] == ("duplicate-user-1@dedup.invalid", False),
    f"A: the dead twin is tombstoned {users[1]}",
)
check(users[4] == ("bob@x.test", True), f"B: the most recent login keeps the address {users[4]}")
check(
    users[3] == ("duplicate-user-3@dedup.invalid", False),
    f"B: the older login is tombstoned {users[3]}",
)
check(users[6] == ("carol@x.test", True), f"C: the active twin keeps the address {users[6]}")
check(
    users[5] == ("duplicate-user-5@dedup.invalid", False),
    f"C: the inactive twin is tombstoned {users[5]}",
)
check(users[7] == ("dave@x.test", True), f"D: with no evidence the lowest id keeps it {users[7]}")
check(
    users[8] == ("duplicate-user-8@dedup.invalid", False), f"D: the other is tombstoned {users[8]}"
)
check(
    users[9][0] == "" and users[9][1], f"empty: the first keeps the empty value, active {users[9]}"
)
for uid in (10, 11):
    check(
        users[uid] == (f"no-email-user-{uid}@dedup.invalid", True),
        f"empty: #{uid} gets a placeholder and STAYS active {users[uid]}",
    )
blob = "\n".join(warnings)
check("#74 DEDUPE" in blob and "user #1 " in blob, "the losers are still logged by id")
check("no email address" in blob, "the placeholdered accounts are logged")

# the unique index exists and holds
eng = create_engine(url)
with eng.connect() as conn:
    idx = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND name='ux_user_email_lower'")
    ).fetchone()
eng.dispose()
check(idx is not None, "ux_user_email_lower was built")

# idempotent: drop only the index, run the data path again, nothing changes
_run(d, "downgrade", _PREV_REV)
_run(d, "upgrade", _REV)
check(_read(url) == users, "downgrade then upgrade again changes nothing")
check(_keys(url) == keys, "downgrade then upgrade again leaves the API keys as they were")

if FAILS:
    print("FAILED:", len(FAILS))
    sys.exit(1)
print("USER EMAIL DEDUPE RULES OK")
