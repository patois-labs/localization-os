"""TM write-back uniqueness + upsert-race verification (QA Part-2 P-C2) — hermetic, offline.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys. The finding: `tm.write_back` did a
read-check-then-INSERT with NOTHING backing uniqueness, so two writers that both pass the
SELECT before either commits both INSERT -> duplicate SegmentTM rows for one source ->
nondeterministic `tm.lookup`. The GIL serialized this away in-process, but a forced
read->insert window produced 2 rows, and the window opens for real under MULTI-PROCESS
writers (server + CLI approving the same source) or a slow disk.

This suite locks in the fix -- the exact branch the whole change exists to serve:

  index present   — ux_segmenttm_upsert_key is UNIQUE on
                    (project_id, source_hash, source_locale, target_locale) on the fresh
                    (create_all + _ensure_indexes) test DB.
  db enforces     — a raw second INSERT of the same key raises IntegrityError (the DB, not
                    just app code, now refuses the duplicate).
  write_back race — a FORCED read->insert window (writer B reads no row, then a concurrent
                    writer A inserts+commits the same key before B's own INSERT) converges
                    to ONE row carrying B's target (last-writer-wins), FK-clean -- proving
                    write_back rolls back its rejected INSERT and re-reads the winner.
  migration dedup — the migration's dedup SQL collapses pre-existing duplicate groups to the
                    newest row (MAX id), deletes the losers' embeddings first, and leaves
                    NULL-project rows untouched (they never block a standard UNIQUE index).
  create_all guard— _ensure_indexes SKIPS (does not crash) when pre-existing dups exist.

Final line: "TM UPSERT RACE OK".
"""

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_c2race_")

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Organization, Project, SegmentEmbedding, SegmentTM
from app.services import tm

SRC, TGT = "en-US", "es-ES"
SOURCE = "The quick brown fox jumps."
IX = "ux_segmenttm_upsert_key"


def _org_id(s):
    return s.exec(select(Organization)).first().id


def _project(s, name="C2"):
    p = Project(name=name, source_locale=SRC, org_id=_org_id(s))
    s.add(p)
    s.commit()
    s.refresh(p)
    return p.id


def _rows(pid):
    with Session(engine) as s:
        return s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all()


def _wipe_tm():
    with engine.connect() as c:
        c.execute(text("DELETE FROM segmentembedding"))
        c.execute(text("DELETE FROM segmenttm"))
        c.commit()


def _fk_clean():
    with engine.connect() as c:
        return not c.execute(text("PRAGMA foreign_key_check")).fetchall()


def test_index_present_and_unique():
    init_db()
    ixs = {i["name"]: i for i in inspect(engine).get_indexes("segmenttm")}
    assert IX in ixs, f"{IX} missing on the fresh test DB: {sorted(ixs)}"
    assert ixs[IX].get("unique"), f"{IX} is not UNIQUE"
    assert list(ixs[IX]["column_names"]) == [
        "project_id",
        "source_hash",
        "source_locale",
        "target_locale",
    ], ixs[IX]["column_names"]
    print(f"  index: {IX} present, UNIQUE, correct column order")


def test_db_rejects_raw_duplicate():
    """The DB itself (not just app code) refuses a second row on the same key."""
    init_db()
    with Session(engine) as s:
        pid = _project(s, "RawDup")
        h = tm.hash_text(SOURCE, SRC, TGT)
        s.add(
            SegmentTM(
                project_id=pid,
                source_locale=SRC,
                target_locale=TGT,
                source_text=SOURCE,
                target_text="one",
                source_hash=h,
            )
        )
        s.commit()
        s.add(
            SegmentTM(
                project_id=pid,
                source_locale=SRC,
                target_locale=TGT,
                source_text=SOURCE,
                target_text="two",
                source_hash=h,
            )
        )
        raised = False
        try:
            s.commit()
        except IntegrityError:
            raised = True
            s.rollback()
    assert raised, "the UNIQUE index did not reject a duplicate raw INSERT"
    assert len(_rows(pid)) == 1, "expected exactly one row after the rejected duplicate"
    print("  db-level: a duplicate raw INSERT raises IntegrityError (one row survives)")


def _forced_window(pid, a_target, b_target):
    """Writer B calls the REAL tm.write_back; at its row construction (post read-check,
    pre commit -- the exact TOCTOU window) a concurrent writer A inserts+commits the same
    key in its own session. Injected via SegmentTM.__init__ so the select(SegmentTM) name
    at the read site is untouched."""
    orig_init = SegmentTM.__init__
    fired = {"done": False}

    def init_hook(self, *a, **k):
        if not fired["done"]:
            fired["done"] = True
            with Session(engine) as sa:  # writer A: a concurrent process, own session
                tm.write_back(sa, pid, SOURCE, a_target, SRC, TGT)
        orig_init(self, *a, **k)

    SegmentTM.__init__ = init_hook
    try:
        with Session(engine) as sb:
            tm.write_back(sb, pid, SOURCE, b_target, SRC, TGT)
            sb.commit()
    finally:
        SegmentTM.__init__ = orig_init


def test_write_back_recovers_from_race():
    init_db()
    with Session(engine) as s:
        pid = _project(s, "Race")
    _forced_window(pid, "A-target", "B-target")
    rows = _rows(pid)
    assert len(rows) == 1, "race left %d rows (expected 1)" % len(rows)  # noqa: UP031
    assert rows[0].target_text == "B-target", (
        f"expected last-writer-wins (B), got {rows[0].target_text!r}"
    )
    assert _fk_clean(), "foreign_key_check found violations after the race"
    print("  write_back: forced read->insert window converges to 1 row (last-writer-wins)")


def _load_migration():
    path = os.path.join(_ROOT, "alembic", "versions", "c2f1a7d4e9b6_segmenttm_upsert_unique.py")
    spec = importlib.util.spec_from_file_location("mig_c2f1", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_dedup_sql():
    """Exercise the migration's OWN dedup SQL (reused verbatim) on a seeded dirty DB:
    keep MAX(id) per non-null group, delete losers' embeddings first, leave NULL-project
    rows alone, then the UNIQUE index builds."""
    init_db()
    _wipe_tm()
    mig = _load_migration()
    # Drop the index up front so the intentional duplicate seeding below is allowed.
    with engine.connect() as c:
        c.execute(text(f"DROP INDEX IF EXISTS {IX}"))
        c.commit()
    with Session(engine) as s:
        pid = _project(s, "Dedup")
        h = tm.hash_text(SOURCE, SRC, TGT)
        # three duplicate rows for one key (ids ascending -> newest is the last)
        ids = []
        for t in ("old1", "old2", "newest"):
            r = SegmentTM(
                project_id=pid,
                source_locale=SRC,
                target_locale=TGT,
                source_text=SOURCE,
                target_text=t,
                source_hash=h,
            )
            s.add(r)
            s.commit()
            s.refresh(r)
            ids.append(r.id)
        # an embedding on a LOSER row (must be deleted before its parent)
        s.add(
            SegmentEmbedding(
                segment_tm_id=ids[0],
                project_id=pid,
                source_locale=SRC,
                target_locale=TGT,
                model="m",
                dim=1,
                embedding=b"\x00\x00\x00\x00",
            )
        )
        s.commit()
        # two NULL-project rows on the same other-columns: must NOT be touched
        for t in ("g1", "g2"):
            s.add(
                SegmentTM(
                    project_id=None,
                    source_locale=SRC,
                    target_locale=TGT,
                    source_text=SOURCE,
                    target_text=t,
                    source_hash=h,
                )
            )
        s.commit()
    # Run the migration's dedup SQL (reused verbatim from the migration module).
    with engine.connect() as c:
        c.execute(text(f"DELETE FROM segmentembedding WHERE segment_tm_id IN ({mig._LOSERS})"))
        c.execute(
            text(
                "DELETE FROM segmenttm WHERE project_id IS NOT NULL AND id NOT IN ("
                "  SELECT MAX(id) FROM segmenttm WHERE project_id IS NOT NULL"
                "  GROUP BY project_id, source_hash, source_locale, target_locale)"
            )
        )
        c.commit()
    survivors = _rows(pid)
    assert len(survivors) == 1 and survivors[0].target_text == "newest", (
        "dedup did not keep the newest row: %r" % [(r.id, r.target_text) for r in survivors]
    )
    with Session(engine) as s:
        nulls = s.exec(select(SegmentTM).where(SegmentTM.project_id.is_(None))).all()
        assert len(nulls) == 2, "NULL-project rows were wrongly deduped: %d" % len(nulls)  # noqa: UP031
        orphans = s.exec(
            select(SegmentEmbedding).where(SegmentEmbedding.segment_tm_id == ids[0])
        ).all()
        assert not orphans, "loser row's embedding was not deleted"
    assert _fk_clean(), "foreign_key_check found violations after dedup"
    # The UNIQUE index now builds cleanly on the deduped (non-null) set.
    with engine.connect() as c:
        c.execute(
            text(
                f"CREATE UNIQUE INDEX {IX} ON segmenttm "
                "(project_id, source_hash, source_locale, target_locale)"
            )
        )
        c.commit()
    print("  migration dedup: newest-wins, losers' embeddings purged, NULL rows kept, index builds")


def test_ensure_indexes_dup_guard():
    """A dirty DB with a pre-existing non-null duplicate group makes _ensure_indexes SKIP
    the index (warn) rather than crash init_db."""
    init_db()
    _wipe_tm()
    with engine.connect() as c:
        c.execute(text(f"DROP INDEX IF EXISTS {IX}"))
        c.commit()
    with Session(engine) as s:
        pid = _project(s, "GuardDup")
        h = tm.hash_text(SOURCE, SRC, TGT)
        for t in ("a", "b"):
            s.add(
                SegmentTM(
                    project_id=pid,
                    source_locale=SRC,
                    target_locale=TGT,
                    source_text=SOURCE,
                    target_text=t,
                    source_hash=h,
                )
            )
        s.commit()
    init_db()  # must NOT raise; the dup-guard skips creating the unique index
    ixs = {i["name"] for i in inspect(engine).get_indexes("segmenttm")}
    assert IX not in ixs, "index should have been skipped while duplicates exist"
    print("  create_all guard: pre-existing dups -> index skipped, init_db does not crash")


def test_migration_upgrade_head_on_seeded_dupes():
    """N1: run the REAL `alembic upgrade head` against a FRESH DB that is seeded with
    duplicate SegmentTM rows AT the pre-migration revision — the exact prod scenario.
    `test_migration_dedup_sql` re-runs the dedup SQL by hand; this proves the migration's
    OWN `upgrade()` orders dedup-before-create_index so the UNIQUE index builds without an
    IntegrityError on a populated-with-dupes install (a mutant that dropped the dedup from
    `upgrade()` would survive every other test but dies here)."""
    import tempfile

    from alembic.config import Config
    from sqlalchemy import create_engine

    from alembic import command

    prev_rev = "d4e5f6a7b8c9"  # this migration's down_revision
    # ignore_cleanup_errors: alembic's env.py opens its own engine on the sqlite file and
    # does not dispose it, so on Windows the file is still locked at TemporaryDirectory
    # teardown (WinError 32). The test's assertions have already run by then; let the temp
    # file leak to the OS rather than fail cleanup.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_dir:
        db_path = os.path.join(data_dir, "loc_tms.db").replace("\\", "/")
        url = "sqlite:///" + db_path
        cfg = Config(os.path.join(_ROOT, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
        cfg.set_main_option("sqlalchemy.url", url)
        # 1) bring the fresh DB up to the revision BEFORE the unique index exists.
        command.upgrade(cfg, prev_rev)
        # 2) seed two rows that collide on the future UNIQUE key (index not yet present).
        eng = create_engine(url)
        with eng.begin() as c:
            for i, tgt in enumerate(("older", "newest")):  # higher id = newest survivor
                c.execute(
                    text(
                        "INSERT INTO segmenttm (project_id, source_locale, target_locale, "
                        "source_text, target_text, source_hash, created_at) VALUES "
                        "(1, :src, :tgt_loc, :s, :t, :h, :ts)"
                    ),
                    {
                        "src": SRC,
                        "tgt_loc": TGT,
                        "s": SOURCE,
                        "t": tgt,
                        "h": "dupehash",
                        "ts": "2026-01-0%d 00:00:00" % (i + 1),  # noqa: UP031
                    },
                )
        eng.dispose()
        # 3) run the REAL migration to head — dedup THEN create_index, in one upgrade().
        command.upgrade(cfg, "head")
        # 4) exactly one survivor (newest), and the UNIQUE index now exists.
        eng = create_engine(url)
        try:
            with eng.connect() as c:
                rows = list(
                    c.execute(
                        text("SELECT target_text FROM segmenttm WHERE source_hash='dupehash'")
                    )
                )
                assert len(rows) == 1, "upgrade() did not dedup: %d rows" % len(rows)  # noqa: UP031
                assert rows[0][0] == "newest", f"wrong survivor: {rows[0][0]!r}"
                ixs = {r[1] for r in c.execute(text("PRAGMA index_list('segmenttm')"))}
                assert IX in ixs, "unique index was not created by upgrade()"
        finally:
            eng.dispose()
    print("  migration upgrade() on seeded dupes: dedup-then-index, 1 newest survivor")


def main():
    test_index_present_and_unique()
    test_db_rejects_raw_duplicate()
    test_write_back_recovers_from_race()
    test_migration_dedup_sql()
    test_migration_upgrade_head_on_seeded_dupes()
    test_ensure_indexes_dup_guard()
    print("TM UPSERT RACE OK")


if __name__ == "__main__":
    main()
