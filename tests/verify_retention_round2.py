"""Round-2 retention regressions, isolated and offline."""

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from threading import Event, Thread
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_retention_round2_")

from sqlalchemy import event, insert, inspect
from sqlmodel import Session

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    FullTextTM,
    Project,
    ProjectTmSource,
    Segment,
    SegmentTM,
    TransMode,
)
from app.services import retention, settings, tm, tm_module


def cli_counts():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.cli import cmd_admin

    emit = Mock()
    preview = {"documents": 1, "segments": 1, "jobs": 0, "files": 1, "key_image_blobs": 0}
    result = {**preview, "tm": 0, "tm_aged": 12, "row_aging_failed": 1, "blob_deletes_pending": 2}
    with (
        patch.object(cmd_admin, "local_op"),
        patch.object(cmd_admin, "confirm", return_value=True),
        patch.object(cmd_admin, "_retention_preview", return_value=preview),
        patch.object(retention, "enabled", return_value=True),
        patch.object(retention, "retention_days", return_value=30),
        patch.object(retention, "purge", return_value=result),
    ):
        assert cmd_admin._retention_purge(SimpleNamespace(yes=True), emit) == 0
    line, payload = emit.result.call_args.args
    assert "12 TM row(s)" in line and "committed batches only" in line, line
    assert "Upload erasure is pending for 2 blob(s)" in line, line
    assert payload["removed"] == result
    confirmation = " ".join(call.args[0] for call in emit.say.call_args_list)
    assert "empties completely" not in confirmation and "SURVIVE" not in confirmation


def activity_migration():
    from io import StringIO

    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text

    from alembic import command

    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    # one linear tree whose history still contains the revision the SQL checks below range over
    # (the tip moves with every later migration; it was pinned to a276b2c3d4e5 until #356)
    assert len(script.get_heads()) == 1
    assert script.get_revision("a276b2c3d4e5") is not None
    with tempfile.TemporaryDirectory() as directory:
        cfg.set_main_option(
            "sqlalchemy.url", "sqlite:///" + directory.replace("\\", "/") + "/mig.db"
        )
        command.upgrade(cfg, "e8a1c4b7d9f2")
        db = create_engine(cfg.get_main_option("sqlalchemy.url"))
        try:
            with db.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO segmenttm (id, source_locale, target_locale, source_text, "
                        "target_text, source_hash, created_at, use_count, last_used_at) "
                        "VALUES (1, 'en', 'fr', 's', 't', 'h', '2020-01-01', 7, '2021-01-01')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO fulltexttm (id, source_locale, target_locale, source_text, "
                        "target_text, source_hash, created_at) "
                        "VALUES (1, 'en', 'fr', 's', 't', 'h', '2020-01-01')"
                    )
                )
            for _ in range(2):
                command.upgrade(cfg, "head")
                with db.connect() as conn:
                    assert conn.execute(
                        text("SELECT activity_at, use_count, last_used_at FROM segmenttm")
                    ).one() == (None, 7, "2021-01-01")
                    assert conn.execute(text("SELECT activity_at FROM fulltexttm")).one() == (None,)
                command.downgrade(cfg, "e8a1c4b7d9f2")
                for table in ("segmenttm", "fulltexttm"):
                    assert "activity_at" not in {c["name"] for c in inspect(db).get_columns(table)}
        finally:
            db.dispose()
    cfg.set_main_option("sqlalchemy.url", "postgresql://offline/offline")
    cfg.output_buffer = StringIO()
    command.upgrade(cfg, "e8a1c4b7d9f2:a276b2c3d4e5", sql=True)
    command.downgrade(cfg, "a276b2c3d4e5:e8a1c4b7d9f2", sql=True)
    sql = cfg.output_buffer.getvalue()
    for table in ("segmenttm", "fulltexttm"):
        assert f"ALTER TABLE {table} ADD COLUMN activity_at TIMESTAMP" in sql
        assert f"ALTER TABLE {table} DROP COLUMN activity_at" in sql
    assert "CREATE INDEX" not in sql and "last_used_at" not in sql
    print("Activity migration: SQLite upgrade/downgrade/re-upgrade and offline Postgres OK")


def activity_paths(pid, old):
    payload = (
        b'<tmx version="1.4"><body><tu><tuv xml:lang="en-US"><seg>identical import</seg></tuv>'
        b'<tuv xml:lang="fr-FR"><seg>same target</seg></tuv></tu></body></tmx>'
    )
    with Session(engine) as s:
        row = tm.write_back(s, pid, "identical import", "same target", "en-US", "fr-FR")
        row.created_at = row.activity_at = old
        s.add(row)
        s.commit()
        rid = row.id
        tm.import_tmx(s, pid, payload, "en-US", "fr-FR")
        s.expire_all()
        row = s.get(SegmentTM, rid)
        assert row.activity_at > old and row.created_at == old and row.last_used_at is None
        full = tm.ft_store(s, pid, "accepted cache", "cached", "en-US", "fr-FR")
        full.created_at = full.activity_at = old
        s.add(full)
        s.commit()
        fid = full.id
        assert tm.ft_lookup(s, pid, "accepted cache", "en-US", "fr-FR") == "cached"
        assert s.get(FullTextTM, fid).activity_at == old, "lookup alone is not acceptance"
        doc = Document(project_id=pid, fmt="txt", filename="cache", mode=TransMode.FULL_TEXT)
        s.add(doc)
        s.flush()
        seg = Segment(
            document_id=doc.id, order_index=0, target_locale="fr-FR", source_text="accepted cache"
        )
        s.add(seg)
        s.commit()
        from app.services import translate

        list(translate.pretranslate_stream(s, doc.id, "fr-FR"))
        s.expire_all()
        assert s.get(Segment, seg.id).target_text == "cached"
        assert s.get(FullTextTM, fid).activity_at > old
        assert s.get(FullTextTM, fid).created_at == old


def main():
    init_db()
    cli_counts()
    activity_migration()
    for table in ("segmenttm", "fulltexttm"):
        columns = inspect(engine).get_columns(table)
        assert any(c["name"] == "activity_at" for c in columns), columns
    old = datetime.utcnow() - timedelta(days=400)
    with Session(engine) as s:
        master, borrower, keys = [Project(name=n) for n in ("master", "borrower", "keys")]
        customer = Customer(name="retention")
        s.add_all([master, borrower, keys, customer])
        s.commit()
        mp, bp, kp = master.id, borrower.id, keys.id
        s.add(ProjectTmSource(project_id=bp, source_project_id=mp))
        for pid, fmt, source, when in [
            (bp, "txt", "borrowed", datetime.utcnow()),
            (kp, "keys", "catalogue", old),
            (kp, "txt", "upload-secret", old),
        ]:
            d = Document(project_id=pid, fmt=fmt, filename="probe", created_at=when)
            s.add(d)
            s.flush()
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="fr-FR",
                    order_index=0,
                    source_text=source,
                    target_text="target",
                )
            )
        rows = {}
        for pid, source in [
            (mp, "borrowed"),
            (kp, "catalogue"),
            (kp, "upload-secret"),
            (bp, "edited"),
            (bp, "approved"),
        ]:
            r = SegmentTM(
                project_id=pid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text=source,
                source_hash=tm.hash_text(source, "en-US", "fr-FR"),
                target_text="old",
                created_at=old,
            )
            s.add(r)
            s.flush()
            rows[source] = r.id
        tm_module.edit_entry(s, rows["edited"], "edited today")
        tm.write_back(s, bp, "approved", "approved today", "en-US", "fr-FR")
        assert s.get(SegmentTM, rows["edited"]).last_used_at is None
        assert s.get(SegmentTM, rows["approved"]).last_used_at is None
    activity_paths(bp, old)
    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    result = retention.purge()
    failures = []
    with Session(engine) as s:
        checks = {
            "P6 attached TM survives": s.get(SegmentTM, rows["borrowed"]) is not None,
            "key upload TM ages": s.get(SegmentTM, rows["upload-secret"]) is None,
            "catalogue survives": s.get(SegmentTM, rows["catalogue"]) is not None,
            "editor edit survives": s.get(SegmentTM, rows["edited"]) is not None,
            "creation time unchanged": s.get(SegmentTM, rows["approved"]).created_at == old,
        }
        for label, ok in checks.items():
            print(label, "PASS" if ok else "FAIL", flush=True)
            if not ok:
                failures.append(label)
    assert not failures, (failures, result)

    # A many-segment project defeats the old quadratic correlated scan.
    with Session(engine) as s:
        d = Document(project_id=bp, fmt="txt", filename="scale")
        s.add(d)
        s.flush()
        s.execute(
            insert(Segment),
            [
                {
                    "document_id": d.id,
                    "target_locale": "fr-FR",
                    "order_index": i,
                    "source_text": f"live-{i}",
                    "target_text": "live",
                }
                for i in range(5000)
            ],
        )
        for start in range(0, 20000, 1000):
            s.execute(
                insert(SegmentTM),
                [
                    {
                        "project_id": bp,
                        "source_locale": "en-US",
                        "target_locale": "fr-FR",
                        "source_text": f"aged-{i}",
                        "source_hash": f"aged-{i}",
                        "target_text": "aged",
                        "created_at": old,
                    }
                    for i in range(start, start + 1000)
                ],
            )
        s.commit()
    started, written = Event(), Event()
    errors, durations = [], []

    def writer():
        if not started.wait(10):
            errors.append("purge never reached TM delete")
            return
        begin = time.perf_counter()
        try:
            with Session(engine) as s:
                s.add(Customer(name="concurrent"))
                s.commit()
        except Exception as exc:
            errors.append(str(exc))
        durations.append(time.perf_counter() - begin)
        written.set()

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM segmenttm"):
            started.set()

    deletes = []

    def deleted(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM segmenttm"):
            assert cursor.rowcount <= 500, cursor.rowcount
            deletes.append(cursor.rowcount)

    def committed(session):
        if "retention_result" in session.info and started.is_set() and not written.is_set():
            assert written.wait(2), "writer blocked after a TM batch commit"

    event.listen(engine, "before_cursor_execute", capture)
    event.listen(engine, "after_cursor_execute", deleted)
    event.listen(Session, "after_commit", committed)
    worker = Thread(target=writer)
    worker.start()
    begin = time.perf_counter()
    try:
        result = retention.purge()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        event.remove(engine, "after_cursor_execute", deleted)
        event.remove(Session, "after_commit", committed)
    elapsed = time.perf_counter() - begin
    worker.join(10)
    print(f"20k TM x 5k segments: {elapsed:.3f}s; concurrent writer: {durations}", flush=True)
    assert elapsed < 5, elapsed
    assert written.is_set() and not errors and max(durations) < 2, errors
    assert result["tm_aged"] == 20000 and not result["row_aging_failed"], result
    assert len(deletes) >= 40, deletes

    # Committed batches remain reported if a later delete fails, and retry drains it.
    with Session(engine) as s:
        s.execute(
            insert(SegmentTM),
            [
                {
                    "project_id": bp,
                    "source_locale": "en-US",
                    "target_locale": "fr-FR",
                    "source_text": f"retry-{i}",
                    "source_hash": f"retry-{i}",
                    "target_text": "old",
                    "created_at": old,
                }
                for i in range(1200)
            ],
        )
        s.commit()
    calls = []

    def fail_second(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM segmenttm"):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("injected second-batch failure")

    event.listen(engine, "before_cursor_execute", fail_second)
    try:
        failed = retention.purge()
    finally:
        event.remove(engine, "before_cursor_execute", fail_second)
    assert failed["row_aging_failed"] == 1 and 0 < failed["tm_aged"] <= 500, failed
    retried = retention.purge()
    assert retried["tm_aged"] + failed["tm_aged"] == 1200, (failed, retried)

    print("verify_retention_round2 OK")


if __name__ == "__main__":
    main()
