"""Language-agnostic workflows, T3 (ADR 0047, D4): clear the inert pair on UNGROUPED workflows.

Alembic revision e3b9d5a1c7f4 is a DATA-ONLY migration:

    UPDATE workflow SET source_locale = NULL, target_locale = NULL
     WHERE group_id IS NULL AND (source_locale IS NOT NULL OR target_locale IS NOT NULL)

Hermetic + offline (throwaway data dirs, never data/loc_tms.db). Each check below fails if the
revision's UPDATE is removed or widened:

  1. fixture DB at the PREVIOUS head: an ungrouped paired workflow, an ungrouped blank one, an
     ungrouped '' (blank-string) one, an ungrouped half-paired one, and a group with variants
     (pairs set, one is_default). After `alembic upgrade head` the ungrouped paired rows have
     BOTH columns NULL, the already-blank one is untouched, EVERY grouped variant keeps its exact
     pair, and `resolve_variant` picks the same variant for each pair before and after.
  2. downgrade is a no-op (nothing changes, no error) and re-upgrading is idempotent: it clears
     a pair added after the first upgrade and still never touches the group.
  3. a REAL install (init_db through alembic, LOC_TMS_SERVER=1, child process) at the previous
     head applies the revision, stamps head, and a second init_db is harmless.
  4. chain + portability: the revision is the single head on top of d1a8c5e3b7f2, its SQL has
     no SQLite-only syntax, and its docstring documents the no-op downgrade.

ASCII-only output.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from tests._bootstrap import init_env

init_env("loctms_pairmig_")

from sqlalchemy import create_engine, text
from sqlmodel import Session

from app.services import workflows as W

PREV_HEAD = "d1a8c5e3b7f2"
THIS_REV = "e3b9d5a1c7f4"

# Portable seed (plain INSERTs, valid at the previous head's schema). Columns not listed are NULL
# or defaulted. ids are explicit so the assertions can name rows.
_SEED_SQL = [
    "INSERT INTO workflowgroup (id, name, description, created_by, created_at) "
    "VALUES (1, 'G', '', '', CURRENT_TIMESTAMP)",
    # ungrouped
    "INSERT INTO workflow (id, name, scope, created_at, is_default, source_locale, target_locale) "
    "VALUES (1, 'ungrouped paired', 'global', CURRENT_TIMESTAMP, 0, 'en-US', 'fr-FR')",
    "INSERT INTO workflow (id, name, scope, created_at, is_default, source_locale, target_locale) "
    "VALUES (2, 'ungrouped blank', 'global', CURRENT_TIMESTAMP, 0, NULL, NULL)",
    "INSERT INTO workflow (id, name, scope, created_at, is_default, source_locale, target_locale) "
    "VALUES (3, 'ungrouped empty strings', 'global', CURRENT_TIMESTAMP, 0, '', '')",
    "INSERT INTO workflow (id, name, scope, created_at, is_default, source_locale, target_locale) "
    "VALUES (4, 'ungrouped half pair', 'global', CURRENT_TIMESTAMP, 0, 'en-US', NULL)",
    # grouped variants (group 1)
    "INSERT INTO workflow (id, name, scope, created_at, is_default, group_id, source_locale, "
    "target_locale) VALUES (5, 'g es', 'global', CURRENT_TIMESTAMP, 0, 1, 'en-US', 'es-ES')",
    "INSERT INTO workflow (id, name, scope, created_at, is_default, group_id, source_locale, "
    "target_locale) VALUES (6, 'g fr default', 'global', CURRENT_TIMESTAMP, 1, 1, 'en-US', "
    "'fr-FR')",
    "INSERT INTO workflow (id, name, scope, created_at, is_default, group_id, source_locale, "
    "target_locale) VALUES (7, 'g de', 'global', CURRENT_TIMESTAMP, 0, 1, 'en-US', 'de-DE')",
    # a grouped variant with only one side set and one with '' (grouped rows are NEVER touched)
    "INSERT INTO workflow (id, name, scope, created_at, is_default, group_id, source_locale, "
    "target_locale) VALUES (8, 'g half', 'global', CURRENT_TIMESTAMP, 0, 1, NULL, 'it-IT')",
    "INSERT INTO workflow (id, name, scope, created_at, is_default, group_id, source_locale, "
    "target_locale) VALUES (9, 'g blank strings', 'global', CURRENT_TIMESTAMP, 0, 1, '', '')",
]

# resolve_variant probes: (workflow id asked, target, source) -> expected id.
_PROBES = {
    (5, "es-ES", "en-US"): 5,
    (5, "fr-FR", "en-US"): 6,
    (5, "de-DE", "en-US"): 7,
    (7, "es-ES", "en-US"): 5,
    (5, "ja-JP", "en-US"): 6,  # no exact variant: the group default
    (1, "fr-FR", "en-US"): 1,  # ungrouped: itself, always
    (3, "de-DE", "en-US"): 3,
}

_GROUPED_PAIRS = {
    5: ("en-US", "es-ES"),
    6: ("en-US", "fr-FR"),
    7: ("en-US", "de-DE"),
    8: (None, "it-IT"),
    9: ("", ""),
}


def _cfg(url: str):
    from alembic.config import Config

    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _fresh_db(prefix: str):
    d = tempfile.mkdtemp(prefix=prefix)
    url = "sqlite:///" + os.path.join(d, "loc_tms.db").replace("\\", "/")
    return d, url


def _pairs(eng) -> dict:
    with eng.connect() as conn:
        rows = conn.execute(
            text("SELECT id, source_locale, target_locale FROM workflow ORDER BY id")
        ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _resolve_all(eng) -> dict:
    out = {}
    with Session(eng) as s:
        for (wid, tgt, src), _exp in _PROBES.items():
            out[(wid, tgt, src)] = W.resolve_variant(s, wid, tgt, src)
    return out


def test_upgrade_clears_ungrouped_keeps_grouped() -> None:
    from alembic import command

    _d, url = _fresh_db("loctms_pairmig_a_")
    cfg = _cfg(url)
    command.upgrade(cfg, PREV_HEAD)
    eng = create_engine(url)
    with eng.begin() as conn:
        for stmt in _SEED_SQL:
            conn.execute(text(stmt))

    before_pairs = _pairs(eng)
    assert before_pairs[1] == ("en-US", "fr-FR") and before_pairs[3] == ("", "")
    before_resolve = _resolve_all(eng)
    assert before_resolve == _PROBES, (
        f"precondition: dispatch before the migration {before_resolve}"
    )

    command.upgrade(cfg, "head")

    after = _pairs(eng)
    assert after[1] == (None, None), f"ungrouped paired not cleared: {after[1]}"
    assert after[2] == (None, None), "ungrouped blank row changed"
    assert after[3] == (None, None), f"ungrouped empty-string pair not cleared: {after[3]}"
    assert after[4] == (None, None), f"ungrouped half pair not cleared: {after[4]}"
    for wid, pair in _GROUPED_PAIRS.items():
        assert after[wid] == pair, f"grouped variant {wid} pair changed: {after[wid]} != {pair}"
    after_resolve = _resolve_all(eng)
    assert after_resolve == before_resolve == _PROBES, (
        f"group dispatch changed: {after_resolve} vs {before_resolve}"
    )
    with eng.connect() as conn:
        default_ids = [
            r[0] for r in conn.execute(text("SELECT id FROM workflow WHERE is_default = 1"))
        ]
        total = conn.execute(text("SELECT COUNT(*) FROM workflow")).scalar()
    assert default_ids == [6], f"is_default touched: {default_ids}"
    assert total == len(_SEED_SQL) - 1, "a workflow row was added or lost"
    eng.dispose()
    print(
        "  upgrade: ungrouped pairs (incl. '' and half pairs) cleared; blank untouched; "
        "all grouped variants keep exact pairs; resolve_variant identical before/after"
    )


def test_downgrade_noop_and_idempotent() -> None:
    from alembic import command

    _d, url = _fresh_db("loctms_pairmig_b_")
    cfg = _cfg(url)
    command.upgrade(cfg, PREV_HEAD)
    eng = create_engine(url)
    with eng.begin() as conn:
        for stmt in _SEED_SQL:
            conn.execute(text(stmt))
    command.upgrade(cfg, "head")
    settled = _pairs(eng)

    # Idempotent: a second upgrade pass over the same data changes nothing. Re-run the revision's
    # own upgrade() through a downgrade (a no-op) + upgrade cycle.
    command.downgrade(cfg, PREV_HEAD)
    assert _pairs(eng) == settled, "downgrade changed data (must be a no-op)"
    with eng.connect() as conn:
        ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert ver == PREV_HEAD, f"downgrade did not move the pointer: {ver}"

    # A pair added to an ungrouped workflow AFTER the first upgrade survives the downgrade...
    with eng.begin() as conn:
        conn.execute(
            text(
                "UPDATE workflow SET source_locale = 'en-US', target_locale = 'ja-JP' WHERE id = 2"
            )
        )
    assert _pairs(eng)[2] == ("en-US", "ja-JP"), "downgrade must not clear anything"
    # ...and the re-upgrade clears it again, grouped rows still untouched.
    command.upgrade(cfg, "head")
    again = _pairs(eng)
    assert again[2] == (None, None), "re-upgrade did not clear the late-added ungrouped pair"
    for wid, pair in _GROUPED_PAIRS.items():
        assert again[wid] == pair, f"re-upgrade touched grouped variant {wid}"
    command.upgrade(cfg, "head")  # at head: a clean no-op
    assert _pairs(eng) == again
    eng.dispose()
    print("  downgrade: no-op, pointer moves back; re-upgrade idempotent, grouped untouched")


_CHILD = r"""
import os, sys
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from alembic.script import ScriptDirectory
from app import config, db
assert not config.IS_TEST, "child must be a REAL install"
cfg = Config("alembic.ini")
cfg.set_main_option("script_location", "alembic")
cfg.set_main_option("sqlalchemy.url", config.DB_URL)
# an existing install parked at the PREVIOUS head, populated
command.upgrade(cfg, %(prev)r)
with db.engine.begin() as conn:
    for stmt in %(seed)r:
        conn.execute(text(stmt))
def snap():
    with db.engine.connect() as c:
        return [tuple(r) for r in c.execute(text(
            "SELECT id, source_locale, target_locale, group_id, is_default FROM workflow "
            "ORDER BY id"))]
pre = snap()
assert pre[0][1:3] == ("en-US", "fr-FR"), pre
db.init_db()   # the real-install upgrade path
post = snap()
with db.engine.connect() as c:
    stamped = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
head = ScriptDirectory.from_config(cfg).get_current_head()
# init_db must bring the install to the CURRENT head; THIS_REV's own effect is proven by the
# data assertions below (ungrouped pairs cleared), so pinning head to THIS_REV literally would
# go stale the moment the next revision stacks on it.
assert stamped == head, (stamped, head)
by = {r[0]: r for r in post}
for i in (1, 2, 3, 4):
    assert by[i][1:3] == (None, None), by[i]
for i, r in {r[0]: r for r in pre}.items():
    if r[3] is not None:
        assert by[i] == r, ("grouped row changed", by[i], r)
db.init_db()   # twice is harmless
assert snap() == post
print("CHILD_OK")
"""


def test_init_db_upgrade_path() -> None:
    d = tempfile.mkdtemp(prefix="loctms_pairmig_c_")
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = d
    env["LOC_TMS_SERVER"] = "1"  # IS_TEST False: init_db takes the alembic authority path
    env.pop("DATABASE_URL", None)
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LOCAL_BASE_URL"):
        env[k] = ""
    code = _CHILD % {"prev": PREV_HEAD, "seed": _SEED_SQL, "rev": THIS_REV}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    tail = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    assert proc.returncode == 0, f"child exited {proc.returncode}:\n{tail}"
    assert "CHILD_OK" in (proc.stdout or ""), f"child did not confirm:\n{tail}"
    print("  init_db: install at the previous head applies the revision; second init_db harmless")


def test_chain_and_portability() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_cfg("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic has multiple heads: {heads}"
    # (verify_evaluations_core precedent) pinning `heads == [THIS_REV]` goes stale the moment
    # the NEXT revision stacks on this one; the durable invariant is ONE head with THIS_REV
    # on the live chain to it.
    assert THIS_REV in {r.revision for r in script.walk_revisions("base", heads[0])}, (
        f"{THIS_REV} is not reachable from the alembic head {heads[0]}"
    )
    rev = script.get_revision(THIS_REV)
    assert rev.down_revision == PREV_HEAD, rev.down_revision
    src = Path(rev.path).read_text(encoding="utf-8")
    sql = " ".join(
        re.findall(r'"([^"\n]*)"', src.split("def upgrade", 1)[1].split("def downgrade")[0])
    )
    assert "UPDATE workflow SET" in sql and "group_id IS NULL" in sql, sql
    for banned in ("PRAGMA", "IFNULL", "GLOB", "rowid", "datetime(", "||", "ILIKE", "::"):
        assert banned not in sql, f"non-portable token {banned!r} in the revision SQL"
    down = src.split("def downgrade", 1)[1]
    assert "pass" in down and "op." not in down, "downgrade must be a documented no-op"
    assert "NO-OP" in src and "cannot be reconstructed" in src, "docstring must say so"
    print("  chain: single head on d1a8c5e3b7f2; portable SQL; downgrade documented no-op")


def main() -> None:
    test_upgrade_clears_ungrouped_keeps_grouped()
    test_downgrade_noop_and_idempotent()
    test_init_db_upgrade_path()
    test_chain_and_portability()
    print("LANG AGNOSTIC PAIR MIGRATION OK")


if __name__ == "__main__":
    main()
