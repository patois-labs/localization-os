"""Issue #615 (last part): glossary CSV and TBX imports commit in bounded chunks (ADR 0061).

Throwaway LOC_TMS_DATA_DIR + blanked provider keys, zero network.

Before this change `glossary_hub._apply_rows` wrote the whole file in ONE transaction: 3.5 s of
SQLite write lock at 100k project-glossary rows and 84 s at 100k MODULE-glossary rows (the concept
reconcile and binding pass dominate), stalling every other writer that long. The write now goes
out in chunks, each a short transaction that re-reads its rows under the lock.

Covers (project glossary and module glossary, CSV and TBX):
  chunk-bound    -- every write transaction holds at most GLOSSARY_WRITE_CHUNK_ROWS (module:
                    GLOSSARY_MODULE_CHUNK_ROWS) entries (INSERT executemany sizes and the commit
                    count are asserted); a file that fits one chunk is exactly one transaction and
                    stays all-or-nothing; a module import ends with one concept per entry.
  lock-free      -- at every pause between two chunks another connection CAN take the SQLite write
                    lock at once (deterministic: no timing).
  adaptive       -- a chunk that is slow to write shrinks the next one (the per-entry cost of a
                    module re-import that moves targets is ten times a fresh insert).
  halfway        -- a failure in chunk 3 of 5 raises GlossaryImportIncomplete (counts and the error
                    CLASS only), keeps the earlier chunks and leaves the session usable;
                    importing the SAME file again stores the rest with no duplicate entry. A failure
                    in the FIRST chunk raises the original error and stores nothing; a file that
                    fits
                    one chunk and fails stores nothing.
  approval       -- a module import that fails after a chunk committed still reverts an APPROVED
                    glossary to draft; a retry that changes nothing does not.
  edit/delete    -- an entry edited or deleted by another writer after the import read the glossary
                    is re-read under its chunk's lock: the file wins where it speaks and the current
                    value stays where it is silent, a delete is not restored and is counted in
                    `concurrent_deleted`, a term another writer created first is updated, not
                    duplicated, and the counts say what the import wrote.
  no-leak        -- a database error (before or after a chunk) and a partial import answer the API,
                    the web, the in-process CLI, `--remote` and (through the API) MCP without SQL or
                    row text; the API answers 500 with partial/rows_written/rows_total.
  surfaces       -- write_chunks and concurrent_deleted reach the API reply, the CLI text and
                    --json (local and --remote) and the web summary.

Final line: "GLOSSARY CHUNKS 615 OK".
"""

import base64
import io
import logging
import os
import sqlite3
import sys
import types
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_glosschunks615_")

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, func, select

from app.cli._common import quiet_import_log
from app.db import engine, init_db
from app.main import app
from app.models import (
    Glossary,
    GlossaryConcept,
    GlossaryEntry,
    GlossarySense,
    Project,
    Role,
    TargetLocale,
    User,
)
from app.services import apikeys, glossary, glossary_hub, settings
from app.services import glossary_module as gm
from tests._cli_runner import run_cli

init_db()
# The injected failures are logged with their traceback on purpose; keep it off the test output.
logging.getLogger("app.services.glossary_hub").setLevel(logging.ERROR)
logging.getLogger("app.web.api_cli_common").setLevel(logging.ERROR)
SRC, TGT = "en-US", "de-DE"
SECRET = "Unit 7 secret row text"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _project(name):
    with Session(engine) as s:
        p = Project(name=name, source_locale=SRC, default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=TGT))
        s.commit()
        return p.id


def _module(name):
    with Session(engine) as s:
        gid = gm.create(s, name).id
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}"])
        return gid


def _csv(indices, prefix="Begriff", extra=None, header="source_term,target_term"):
    lines = [header]
    for i in indices:
        lines.append(f"gl615 term {i},{prefix} {i}")
    lines += extra or []
    return ("\n".join(lines) + "\n").encode()


def _tbx(indices, prefix="Begriff"):
    parts = [
        '<?xml version="1.0" encoding="utf-8"?><martif type="TBX" xml:lang="en-US">'
        "<martifHeader><fileDesc><titleStmt><title>t</title></titleStmt>"
        "<sourceDesc><p>t</p></sourceDesc></fileDesc></martifHeader><text><body>"
    ]
    for i in indices:
        parts.append(
            f'<termEntry id="c{i}"><langSet xml:lang="{SRC}"><ntig><termGrp><term>gl615 term {i}'
            f"</term></termGrp></ntig></langSet>"
            f'<langSet xml:lang="de"><ntig><termGrp><term>{prefix} {i}</term></termGrp></ntig>'
            f"</langSet></termEntry>"
        )
    parts.append("</body></text></martif>")
    return "".join(parts).encode("utf-8")


def _entries(scope_pid=None, gid=None):
    with Session(engine) as s:
        q = select(GlossaryEntry)
        q = (
            q.where(GlossaryEntry.glossary_id == gid)
            if gid
            else q.where(
                GlossaryEntry.project_id == scope_pid,
                GlossaryEntry.glossary_id == None,  # noqa: E711
            )
        )
        return {e.source_term: e for e in s.exec(q).all()}


def _n(pid=None, gid=None):
    return len(_entries(pid, gid))


def _distinct(pid=None, gid=None):
    with Session(engine) as s:
        q = select(func.count(func.distinct(GlossaryEntry.source_term)))
        q = (
            q.where(GlossaryEntry.glossary_id == gid)
            if gid
            else q.where(
                GlossaryEntry.project_id == pid,
                GlossaryEntry.glossary_id == None,  # noqa: E711
            )
        )
        return s.exec(q).one()


def _concepts(gid):
    with Session(engine) as s:
        return s.exec(
            select(func.count())
            .select_from(GlossaryConcept)
            .where(GlossaryConcept.glossary_id == gid)
        ).one()


def _write_now() -> bool:
    """Can ANOTHER connection take the write lock right now? (0.2 s busy timeout.)"""
    con = sqlite3.connect(engine.url.database, timeout=0.2, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=200")
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TABLE IF NOT EXISTS lock_probe_gl615 (t REAL)")
        con.execute("INSERT INTO lock_probe_gl615 VALUES (0)")
        con.execute("COMMIT")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


class _Knobs:
    """Fixed chunk sizes (no adaptation) and a pause hook for one test; restored after."""

    def __init__(self, rows, pause=0.0, sleeper=None, target=0.0):
        self.rows, self.pause, self.sleeper, self.target = rows, pause, sleeper, target

    def __enter__(self):
        h = glossary_hub
        self.old = (
            h.GLOSSARY_WRITE_CHUNK_ROWS,
            h.GLOSSARY_MODULE_CHUNK_ROWS,
            h.GLOSSARY_CHUNK_PAUSE_S,
            h.GLOSSARY_CHUNK_TARGET_S,
            h.time,
        )
        h.GLOSSARY_WRITE_CHUNK_ROWS = h.GLOSSARY_MODULE_CHUNK_ROWS = self.rows
        h.GLOSSARY_CHUNK_PAUSE_S, h.GLOSSARY_CHUNK_TARGET_S = self.pause, self.target
        if self.sleeper is not None:
            h.time = types.SimpleNamespace(sleep=self.sleeper, monotonic=self.old[4].monotonic)
        return self

    def __exit__(self, *exc):
        h = glossary_hub
        (
            h.GLOSSARY_WRITE_CHUNK_ROWS,
            h.GLOSSARY_MODULE_CHUNK_ROWS,
            h.GLOSSARY_CHUNK_PAUSE_S,
            h.GLOSSARY_CHUNK_TARGET_S,
            h.time,
        ) = self.old
        return False


class _FailNthInsert:
    """Make the nth `INSERT INTO glossaryentry` raise (a failure inside chunk n)."""

    def __init__(self, nth, exc=None):
        self.nth, self.seen, self.exc = nth, 0, exc

    def _hook(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO GLOSSARYENTRY"):
            self.seen += 1
            if self.seen == self.nth:
                if self.exc is not None:
                    raise self.exc(statement, [(SECRET,)], Exception("disk"))
                raise RuntimeError("injected chunk failure")

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._hook)
        return False


_KEY = []


def _key():
    if not _KEY:
        with Session(engine) as s:
            u = User(email="gl615-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id
        _row, raw = apikeys.create_key(uid, "gl615", scopes="api docs")
        _KEY.append(raw)
    return _KEY[0]


def _import(pid, gid, data, filename="g.csv", session=None):
    def run(s):
        if gid:
            return gm.import_bytes(s, gid, data, SRC, TGT, filename=filename)
        return glossary.import_bytes(s, pid, data, SRC, TGT, filename=filename)

    if session is not None:
        return run(session)
    with Session(engine) as s:
        return run(s)


# --------------------------------------------------------------------------- tests


def test_chunk_bound_every_flavour():
    sizes, commits = [], []

    def spy(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO GLOSSARYENTRY") and executemany:
            sizes.append(len(parameters))

    def on_commit(conn):
        commits.append(1)

    event.listen(engine, "before_cursor_execute", spy)
    event.listen(engine, "commit", on_commit)
    try:
        for label, is_module, make, name in (
            ("project csv", False, _csv, "g.csv"),
            ("project tbx", False, _tbx, "g.tbx"),
            ("module csv", True, _csv, "g.csv"),
            ("module tbx", True, _tbx, "g.tbx"),
        ):
            pid = None if is_module else _project(f"glc615-bound-{label}")
            gid = _module(f"glc615-bound-{label}") if is_module else None
            sizes.clear()
            commits.clear()
            with _Knobs(100):
                res = _import(pid, gid, make(range(1050)), name)
            if res["entries"] != 1050 or res["write_chunks"] != 11 or res["concurrent_deleted"]:
                _fail(f"{label}: 1050 new entries at 100 per chunk must be 11 chunks: {res}")
            if not sizes or max(sizes) > 100:
                _fail(f"{label}: an INSERT carried more rows than the chunk bound: {set(sizes)}")
            if len(commits) < 11:
                _fail(f"{label}: each chunk must be its own commit, saw {len(commits)}")
            if _n(pid, gid) != 1050 or _distinct(pid, gid) != 1050:
                _fail(f"{label}: every term must be stored exactly once")
            if is_module and _concepts(gid) != 1050:
                _fail(f"{label}: a module import must end with one concept per entry")
            # a re-import moving 250 targets: same chunking, nothing duplicated, counts exact
            if name.endswith("csv"):
                with _Knobs(100):
                    res2 = _import(
                        pid, gid, _csv(range(1050), "Neu") if False else _csv_mix(), name
                    )
                if res2["entries"] or res2["updated"] != 1050 or res2["changed"] != 250:
                    _fail(f"{label}: mixed re-import must change 250 of 1050: {res2}")
                if res2["unchanged"] != 800 or res2["write_chunks"] != 11:
                    _fail(f"{label}: mixed re-import counts/chunks: {res2}")
                if _n(pid, gid) != 1050:
                    _fail(f"{label}: a re-import must not add entries")
            # a file that fits one chunk is one transaction
            pid2 = None if is_module else _project(f"glc615-one-{label}")
            gid2 = _module(f"glc615-one-{label}") if is_module else None
            with _Knobs(100):
                one = _import(pid2, gid2, make(range(100)), name)
            if one["write_chunks"] != 1:
                _fail(f"{label}: a file under the chunk bound must be a single transaction: {one}")
    finally:
        event.remove(engine, "before_cursor_execute", spy)
        event.remove(engine, "commit", on_commit)
    print("  chunk-bound: project/module x csv/tbx, 1050 rows -> 11 chunks of <= 100; small = 1 OK")


def _csv_mix():
    """1050 terms; the first 250 get a new target, the other 800 are identical."""
    lines = ["source_term,target_term"]
    for i in range(1050):
        lines.append(f"gl615 term {i},{'Neu' if i < 250 else 'Begriff'} {i}")
    return ("\n".join(lines) + "\n").encode()


def test_lock_free_between_chunks():
    for is_module in (False, True):
        pid = None if is_module else _project("glc615-free")
        gid = _module("glc615-free") if is_module else None
        results = []

        def sleeper(_s, results=results):
            results.append(_write_now())

        with _Knobs(100, pause=0.001, sleeper=sleeper):
            res = _import(pid, gid, _csv(range(450)))
        if res["write_chunks"] != 5 or len(results) != 4:
            _fail(f"5 chunks must pause 4 times: {res['write_chunks']} chunks, {len(results)}")
        if not all(results):
            _fail("the write lock was still held at a pause between chunks")
    print("  lock-free: another connection took the write lock at every pause (project, module) OK")


def test_chunks_shrink_when_slow():
    """The chunk is sized from how long the previous one held the lock: with a clock that says
    every chunk took a second, the target (0.15 s) makes the next chunk smaller."""
    pid = _project("glc615-adapt")
    seen = []
    h = glossary_hub
    real = h._chunk_write

    def spying(session, **kw):
        seen.append(len(kw["part_new"]) + len(kw["part_ex"]))
        return real(session, **kw)

    ticks = iter(range(0, 10_000, 1))  # every monotonic() call is 1 s after the last
    clock = types.SimpleNamespace(sleep=lambda _s: None, monotonic=lambda: float(next(ticks)))
    h._chunk_write = spying
    old = (h.GLOSSARY_WRITE_CHUNK_ROWS, h.GLOSSARY_CHUNK_PAUSE_S, h.GLOSSARY_CHUNK_TARGET_S, h.time)
    h.GLOSSARY_WRITE_CHUNK_ROWS, h.GLOSSARY_CHUNK_PAUSE_S, h.GLOSSARY_CHUNK_TARGET_S = 500, 0, 0.15
    h.time = clock
    try:
        res = _import(pid, None, _csv(range(700)))
    finally:
        h._chunk_write = real
        (
            h.GLOSSARY_WRITE_CHUNK_ROWS,
            h.GLOSSARY_CHUNK_PAUSE_S,
            h.GLOSSARY_CHUNK_TARGET_S,
            h.time,
        ) = old
    if res["entries"] != 700 or _n(pid) != 700:
        _fail(f"adaptive import must store everything once: {res}")
    if seen[0] != 100 or seen[1] >= seen[0] or seen[-1] > 2:
        _fail(f"slow chunks must shrink the next one (start 100, then smaller): {seen[:6]}")
    print(f"  adaptive: a slow chunk shrinks the next ({seen[:4]}...) and the file still lands OK")


def test_failure_halfway_then_retry():
    for is_module in (False, True):
        label = "module" if is_module else "project"
        pid = None if is_module else _project(f"glc615-half-{label}")
        gid = _module(f"glc615-half-{label}") if is_module else None
        doc = _csv(range(450))
        with _Knobs(100), Session(engine) as s:
            with _FailNthInsert(3):
                try:
                    _import(pid, gid, doc, session=s)
                except glossary.GlossaryImportIncomplete as exc:
                    msg = str(exc)
                    got = (exc.rows_written, exc.rows_total)
                else:
                    _fail(f"{label}: chunk-3 failure must raise GlossaryImportIncomplete")
            if got != (200, 450) or "RuntimeError" not in msg or "injected" in msg:
                _fail(
                    f"{label}: honest counts and the error class only, never its text: {got} {msg}"
                )
            if "again" not in msg or "no duplicates" not in msg:
                _fail(f"{label}: the message must say how to finish: {msg}")
            if not s.exec(select(func.count()).select_from(Project)).one():
                _fail("the session must stay usable after a partial import")
            if _n(pid, gid) != 200:
                _fail(f"{label}: the two committed chunks must stay: {_n(pid, gid)}")
            res = _import(pid, gid, doc, session=s)  # retry the same file
        if _n(pid, gid) != 450 or _distinct(pid, gid) != 450:
            _fail(f"{label}: the retry must finish the job with no duplicate entry")
        if res["entries"] != 250 or res["updated"] != 200 or res["changed"] != 0:
            _fail(f"{label}: the retry stores the missing 250 and finds 200 unchanged: {res}")
        if is_module and _concepts(gid) != 450:
            _fail(f"{label}: concept store must match the entries after the retry")
        # a failure in the FIRST chunk re-raises the original error and stores nothing
        pid2 = None if is_module else _project(f"glc615-first-{label}")
        gid2 = _module(f"glc615-first-{label}") if is_module else None
        with _Knobs(100), _FailNthInsert(1):
            try:
                _import(pid2, gid2, doc)
            except RuntimeError as exc:
                if "injected chunk failure" not in str(exc):
                    _fail("a first-chunk failure must surface the original error")
            except Exception as exc:
                _fail(f"{label}: first-chunk failure must raise the original error, not {exc!r}")
            else:
                _fail(f"{label}: the injected failure did not fire")
        if _n(pid2, gid2) != 0:
            _fail(f"{label}: a first-chunk failure must store nothing")
        # a file that fits one chunk is all-or-nothing
        pid3 = None if is_module else _project(f"glc615-small-{label}")
        gid3 = _module(f"glc615-small-{label}") if is_module else None
        with _Knobs(500), _FailNthInsert(1):
            try:
                _import(pid3, gid3, doc)
            except RuntimeError:
                pass
            else:
                _fail("the injected failure did not fire")
        if _n(pid3, gid3) != 0:
            _fail(f"{label}: a one-chunk file that fails must store nothing")
    print(
        "  halfway: chunk-3 failure keeps 200 of 450, retry finishes with no duplicates (both) OK"
    )


def test_partial_failure_reverts_approval():
    gid = _module("glc615-approval")
    with Session(engine) as s:
        _import(None, gid, _csv(range(10), "Alt"), session=s)
        gm.approve(s, gid, "a@x.io")
        status_before = s.get(Glossary, gid).status
    with _Knobs(100), Session(engine) as s, _FailNthInsert(3):
        try:
            _import(None, gid, _csv(range(10, 450)), session=s)
        except glossary.GlossaryImportIncomplete:
            pass
        else:
            _fail("the partial import must raise")
    with Session(engine) as s:
        status_after = s.get(Glossary, gid).status
    if status_before == status_after:
        _fail("a partial import that stored new terms must revert an APPROVED glossary to draft")
    # a retry that changes nothing leaves an approved glossary approved
    with Session(engine) as s:
        _import(None, gid, _csv(range(10, 450)), session=s)
        gm.approve(s, gid, "a@x.io")
    with Session(engine) as s:
        res = _import(None, gid, _csv(range(10, 450)), session=s)
        if res["changed"] or res["entries"]:
            _fail(f"an idempotent re-import must report no change: {res}")
        if s.get(Glossary, gid).status != status_before:
            _fail("an idempotent re-import must keep an approved glossary approved")
    print("  approval: a partial import reverts approved to draft; an idempotent retry does not OK")


def _target_of(pid, i, gid=None):
    e = _entries(pid, gid).get(f"gl615 term {i}")
    return None if e is None else e.target_term


def test_edit_or_delete_during_import_is_reread_and_counted():
    for is_module in (False, True):
        label = "module" if is_module else "project"
        pid = None if is_module else _project(f"glc615-edit-{label}")
        gid = _module(f"glc615-edit-{label}") if is_module else None
        _import(pid, gid, _csv(range(450)))
        ids = {e.source_term: e.id for e in _entries(pid, gid).values()}
        fired = []

        def sleeper(_s, pid=pid, gid=gid, ids=ids, fired=fired):
            if fired:
                return
            fired.append(1)
            with Session(engine) as other:  # chunk 1 (terms 0..99) is committed
                e300 = other.get(GlossaryEntry, ids["gl615 term 300"])
                e300.target_term, e300.dnt = "Linguist 300", True
                other.delete(other.get(GlossaryEntry, ids["gl615 term 301"]))
                other.get(GlossaryEntry, ids["gl615 term 302"]).target_term = "Neu 302"
                other.get(GlossaryEntry, ids["gl615 term 420"]).target_term = "Linguist 420"
                other.commit()

        doc = _csv_edit()
        with _Knobs(100, pause=0.001, sleeper=sleeper):
            res = _import(pid, gid, doc)
        if not fired:
            _fail("the edit hook never ran")
        if _target_of(pid, 300, gid) != "Neu 300" or _target_of(pid, 420, gid) != "Begriff 420":
            _fail(f"{label}: an edited row must end with the file's target (the file wins)")
        if _entries(pid, gid)["gl615 term 300"].dnt is not True:
            _fail(f"{label}: a field the file is silent about must keep the other writer's value")
        if _target_of(pid, 301, gid) is not None or _n(pid, gid) != 449:
            _fail(f"{label}: an entry deleted while the import ran was brought back")
        if _target_of(pid, 299, gid) != "Neu 299" or _target_of(pid, 0, gid) != "Neu 0":
            _fail(f"{label}: entries nobody touched must still get the file's target")
        want = {
            "entries": 0,
            "updated": 449,
            "changed": 399,
            "unchanged": 50,
            "concurrent_deleted": 1,
        }
        got = {k: res[k] for k in want}
        if got != want:
            _fail(f"{label}: the counts must say what this import wrote: {got} != {want}")
        if is_module:
            with Session(engine) as s:
                s.commit()
    print("  edit/delete: re-read per chunk, file wins where it speaks, delete not restored OK")


def _csv_edit():
    """450 existing terms; the first 400 get 'Neu', the other 50 are identical to what is stored."""
    lines = ["source_term,target_term"]
    for i in range(450):
        lines.append(f"gl615 term {i},{'Neu' if i < 400 else 'Begriff'} {i}")
    return ("\n".join(lines) + "\n").encode()


def test_term_created_by_another_writer_is_updated_not_duplicated():
    for is_module in (False, True):
        label = "module" if is_module else "project"
        pid = None if is_module else _project(f"glc615-late-{label}")
        gid = _module(f"glc615-late-{label}") if is_module else None
        fired = []

        def sleeper(_s, pid=pid, gid=gid, fired=fired):
            if fired:
                return
            fired.append(1)
            with Session(engine) as other:
                other.add(
                    GlossaryEntry(
                        project_id=None if gid else pid,
                        glossary_id=gid,
                        source_locale=SRC,
                        target_locale=TGT,
                        source_term="gl615 term 250",
                        target_term="Other writer",
                        dnt=True,
                    )
                )
                other.commit()
            if gid:  # the module path keeps its concept store in step, as an editor would
                with Session(engine) as other:
                    from app.services import glossary_concepts as gc

                    for e in other.exec(
                        select(GlossaryEntry).where(GlossaryEntry.source_term == "gl615 term 250")
                    ).all():
                        gc.sync_entry_concept(other, e)
                    other.commit()

        with _Knobs(100, pause=0.001, sleeper=sleeper):
            res = _import(pid, gid, _csv(range(300)))
        if not fired:
            _fail("the writer hook never ran")
        if _n(pid, gid) != 300 or _distinct(pid, gid) != 300:
            _fail(f"{label}: a term another writer created first was duplicated")
        if _target_of(pid, 250, gid) != "Begriff 250":
            _fail(f"{label}: the file's target must win on the term the other writer created")
        if _entries(pid, gid)["gl615 term 250"].dnt is not True:
            _fail(f"{label}: a field the file is silent about keeps the other writer's value")
        if (res["entries"], res["updated"], res["changed"]) != (299, 1, 1):
            _fail(f"{label}: counts must say 299 new and 1 updated: {res}")
        if is_module and _concepts(gid) != 300:
            _fail(f"{label}: one concept per entry expected, got {_concepts(gid)}")
    print("  late term: created by another writer meanwhile -> updated once, counts 299 + 1 OK")


def test_senses_survive_chunking():
    """A sense-bearing CSV chunks like any other: senses follow their entry; re-import: no-op."""
    for is_module in (False, True):
        label = "module" if is_module else "project"
        pid = None if is_module else _project(f"glc615-sense-{label}")
        gid = _module(f"glc615-sense-{label}") if is_module else None
        lines = ["source_term,target_term,sense_label,context_cue"]
        for i in range(300):
            lines.append(f"gl615 term {i},Begriff {i},,")
            if i % 3 == 0:
                lines.append(f"gl615 term {i},Sinn {i},finance,money")
        doc = ("\n".join(lines) + "\n").encode()
        with _Knobs(100):
            res = _import(pid, gid, doc)
            again = _import(pid, gid, doc)
        if res["entries"] != 300 or res["senses"] != 100 or res["write_chunks"] < 3:
            _fail(f"{label}: sense import counts/chunks: {res}")
        if again["entries"] or again["senses"] or again["changed"]:
            _fail(f"{label}: a sense-bearing re-import must be a no-op: {again}")
        with Session(engine) as s:
            n_sense = len(
                s.exec(
                    select(GlossarySense)
                    .join(GlossaryEntry, GlossarySense.glossary_entry_id == GlossaryEntry.id)
                    .where(
                        GlossaryEntry.glossary_id == gid if gid else GlossaryEntry.project_id == pid
                    )
                ).all()
            )
        if n_sense != 100:
            _fail(f"{label}: expected 100 stored senses, found {n_sense}")
    print("  senses: 100 senses follow their entries across chunks; re-import is a no-op OK")


class _Tail:
    def __init__(self):
        self.parts = []


def _run_remote(argv):
    from app.cli import _remote
    from app.cli.__main__ import main as cli_main

    tc = TestClient(app, raise_server_exceptions=False)

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = tc.request(request.method, path, headers=dict(request.headers), content=request.content)
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": r.headers.get("content-type", "")},
        )

    _remote._TRANSPORT = httpx.MockTransport(handler)
    out, err = io.StringIO(), io.StringIO()
    from contextlib import redirect_stderr

    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(["--remote", "http://127.0.0.1", "--token", _key(), *argv])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def test_no_internals_leak_on_any_surface():
    pid = _project("glc615-leak")
    gid = _module("glc615-leak")
    h = {"X-Loc-Token": _key()}
    c = TestClient(app, raise_server_exceptions=False)
    doc = _csv(range(450))
    b64 = base64.b64encode(doc).decode()
    payload = {"csv_b64": b64, "source": SRC, "target": TGT, "filename": "g.csv"}

    # (1) a PARTIAL import: 500 + partial + counts, error class only, on both API routes
    for url in (f"/api/projects/{pid}/glossary/import", f"/api/glossaries/{gid}/import"):
        with _Knobs(100), _FailNthInsert(3):
            r = c.post(url, json=payload, headers=h)
        body = r.json()
        if r.status_code != 500 or body.get("partial") is not True:
            _fail(
                f"{url}: a partial import must answer 500 with partial=true: {r.status_code} {body}"
            )
        if body.get("rows_total") != 450 or body.get("rows_written") != 200:
            _fail(f"{url}: the API must report the counts: {body}")
        if "injected chunk failure" in r.text or "INSERT" in r.text:
            _fail(f"{url}: the partial reply leaked internals: {r.text[:300]}")
    # (2) a DATABASE error before anything committed: generic 500, nothing stored, no SQL/row text
    pid3, gid3 = _project("glc615-leak-db"), _module("glc615-leak-db")
    for url, _scope in (
        (f"/api/projects/{pid3}/glossary/import", pid3),
        (f"/api/glossaries/{gid3}/import", gid3),
    ):
        with _Knobs(100), _FailNthInsert(1, OperationalError):
            r = c.post(
                url,
                json={**payload, "csv_b64": base64.b64encode(_csv(range(50))).decode()},
                headers=h,
            )
        if r.status_code != 500 or r.json().get("ok") is not False:
            _fail(f"{url}: a database error must answer 500: {r.status_code} {r.text[:300]}")
        if "INSERT" in r.text or "secret row text" in r.text or "Unit 7" in r.text:
            _fail(f"{url}: the reply leaked the SQL or row text: {r.text[:300]}")
    if _n(pid3) or _n(None, gid3):
        _fail("a first-chunk database error must store nothing")
    # (3) the web: a partial import redirects with the counts, a database error generically
    pid4, gid4 = _project("glc615-leak-web"), _module("glc615-leak-web")
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    try:
        form = {"source_locale": SRC, "target_locale": TGT}
        files = {"file": ("g.csv", doc, "text/csv")}
        for url in (f"/projects/{pid4}/glossary/import", f"/glossaries/{gid4}/import"):
            with _Knobs(100), _FailNthInsert(3):
                w = c.post(url, data=form, files=files, follow_redirects=False)
            loc = w.headers.get("location", "")
            if w.status_code != 303:
                _fail(f"{url}: {w.status_code} {loc}")
            page = c.get(loc).text
            if "stopped after 200 of 450" not in page or "Those entries are kept" not in page:
                _fail(f"{url}: the page must say how far the import got: {loc[:200]}")
            if "injected" in loc or "INSERT" in loc or "injected" in page:
                _fail(f"{url}: the web leaked internals")
        pid5 = _project("glc615-leak-web-db")
        with _Knobs(100), _FailNthInsert(1, OperationalError):
            w = c.post(
                f"/projects/{pid5}/glossary/import", data=form, files=files, follow_redirects=False
            )
        if "INSERT" in w.headers.get("location", "") or "secret" in w.headers.get("location", ""):
            _fail("the web redirect leaked the SQL")
    finally:
        settings.set_many({"security_auth_required": "1"})
        settings.invalidate_cache()
    # (4) the in-process CLI
    path = os.path.join(_TMP, "g450.csv")
    with open(path, "wb") as fh:
        fh.write(doc)
    pid6, gid6 = _project("glc615-leak-cli"), _module("glc615-leak-cli")
    for argv in (
        ["glossary", "import", str(pid6), path],
        ["glossary", "module", "import", str(gid6), path],
    ):
        with _Knobs(100), _FailNthInsert(3):
            code, out, err = run_cli(["--json", *argv, "--source", SRC, "--target", TGT])
        text = out + err
        if code == 0 or "stopped after 200 of 450" not in text or '"rows_written": 200' not in text:
            _fail(f"cli {argv[:3]}: partial must exit non-zero with counts: {code} {text[:300]}")
        if "injected chunk failure" in text:
            _fail("the CLI leaked the error text of a partial import")
    pid7, gid7 = _project("glc615-leak-cli-db"), _module("glc615-leak-cli-db")
    for argv in (
        ["glossary", "import", str(pid7), path],
        ["glossary", "module", "import", str(gid7), path],
    ):
        with _Knobs(100), _FailNthInsert(1, OperationalError):
            code, out, err = run_cli([*argv, "--source", SRC, "--target", TGT])
        text = out + err
        if (
            code == 0
            or "INSERT" in text
            or "secret row text" in text
            or "database error" not in text
        ):
            _fail(
                f"cli {argv[:3]}: a database error must exit non-zero, no SQL: {code} {text[:300]}"
            )
    # (5) --remote forwards the API's partial answer
    pid8, gid8 = _project("glc615-leak-remote"), _module("glc615-leak-remote")
    for argv in (
        ["glossary", "import", str(pid8), path],
        ["glossary", "module", "import", str(gid8), path],
    ):
        # (the app runs in this process, so its own log line would land on the same stderr)
        with _Knobs(100), _FailNthInsert(3), quiet_import_log():
            code, out, err = _run_remote([*argv, "--source", SRC, "--target", TGT])
        text = out + err
        if code == 0 or "stopped after 200 of 450" not in text:
            _fail(f"remote {argv[:3]}: partial must exit non-zero with counts: {code} {text[:300]}")
        if "injected chunk failure" in text or "INSERT" in text:
            _fail("the remote CLI leaked internals")
    # (6) MCP edit_glossary action=import (it forwards to the module route): the partial answer
    # reaches the model as a tool error with the counts and no internals
    import asyncio
    import pathlib

    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import server as mcp_server

    mcp_server.BASE_URL = "http://testserver"
    mcp_server.TOKEN = _key()
    mcp_server.OUTPUT_DIR = pathlib.Path(_TMP)
    mcp_server._client = lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), timeout=60
    )
    gid9 = _module("glc615-leak-mcp")
    args = {
        "action": "import",
        "glossary_id": gid9,
        "data": doc.decode(),
        "source": SRC,
        "target": TGT,
    }
    with _Knobs(100), _FailNthInsert(3):
        try:
            asyncio.run(mcp_server.call_tool_unvalidated("edit_glossary", args))
        except Exception as exc:
            text = str(exc)
        else:
            _fail("mcp edit_glossary: a partial import must raise a tool error")
    if "stopped after 200 of 450" not in text:
        _fail(f"mcp edit_glossary: the tool error must carry the counts: {text[:300]}")
    if "injected chunk failure" in text or "INSERT" in text:
        _fail("mcp edit_glossary leaked internals")
    print("  no-leak: partial = 500 + counts, db error = generic; api, web, cli, remote, mcp OK")


def test_surfaces_carry_chunks_and_deletes():
    pid = _project("glc615-surf")
    gid = _module("glc615-surf")
    h = {"X-Loc-Token": _key()}
    c = TestClient(app, raise_server_exceptions=False)
    big = base64.b64encode(_csv(range(30))).decode()
    with _Knobs(10):
        r = c.post(
            f"/api/projects/{pid}/glossary/import",
            json={"csv_b64": big, "source": SRC, "target": TGT},
            headers=h,
        )
        r2 = c.post(
            f"/api/glossaries/{gid}/import",
            json={"csv_b64": big, "source": SRC, "target": TGT, "filename": "g.csv"},
            headers=h,
        )
    for body in (r.json(), r2.json()):
        if body.get("write_chunks") != 3 or body.get("concurrent_deleted") != 0:
            _fail(f"the API reply must carry write_chunks and concurrent_deleted: {body}")
    path = os.path.join(_TMP, "g30.csv")
    with open(path, "wb") as fh:
        fh.write(_csv(range(30, 60)))
    pid2, gid2 = _project("glc615-surf-cli"), _module("glc615-surf-cli")
    for label, runner in (("local", run_cli), ("remote", _run_remote)):
        for argv in (
            ["glossary", "import", str(pid2), path],
            ["glossary", "module", "import", str(gid2), path],
        ):
            with _Knobs(10):
                code, out, _err = runner([*argv, "--source", SRC, "--target", TGT])
            if code != 0 or "Written in 3 short transactions" not in out:
                _fail(f"{label} cli {argv[:3]}: the text must name the chunks: {code} {out[:300]}")
            with _Knobs(10):
                code, out, _err = runner(["--json", *argv, "--source", SRC, "--target", TGT])
            if code != 0 or '"write_chunks": 3' not in out or '"concurrent_deleted": 0' not in out:
                _fail(f"{label} cli {argv[:3]} --json must carry both fields: {out[:300]}")
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    try:
        p1 = c.get(f"/projects/{pid}?gl_entries=3&gl_senses=0&gl_deleted=2")
        p2 = c.get(f"/glossaries/{gid}?gl_entries=3&gl_senses=0&gl_deleted=1")
    finally:
        settings.set_many({"security_auth_required": "1"})
        settings.invalidate_cache()
    if "did not restore 2 entries someone deleted while the import ran" not in p1.text:
        _fail("the project page summary must name the deleted entries")
    if "Did not restore 1 entry someone deleted while the import ran" not in p2.text:
        _fail("the glossary page summary must name the deleted entry")
    print(
        "  surfaces: write_chunks/concurrent_deleted on api x2, cli x2 (local, remote), web x2 OK"
    )


def test_term_columns_pass_is_batched():
    """The six concept-model columns of a module CSV are applied per batch (one lookup per 500
    surfaces, one commit per batch), not one glossary scan per row."""
    gid = _module("glc615-termcols")
    lines = ["source_term,target_term,status"]
    for i in range(30):
        lines.append(f"gl615 term {i},Begriff {i},{'admitted' if i % 2 else 'preferred'}")
    doc = ("\n".join(lines) + "\n").encode()
    per_row = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        flat = " ".join(statement.split()).lower()
        if "from glossaryentry" in flat and "glossaryentry.source_term = ?" in flat:
            per_row.append(1)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        with _Knobs(10):
            res = _import(None, gid, doc)
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    if res["entries"] != 30:
        _fail(f"term-column import: {res}")
    if per_row:
        _fail(f"the term-column pass looked entries up one row at a time ({len(per_row)} lookups)")
    from app.models import GlossaryTerm

    with Session(engine) as s:
        statuses = {
            t.term: t.status
            for t in s.exec(
                select(GlossaryTerm)
                .join(GlossaryConcept, GlossaryTerm.concept_id == GlossaryConcept.id)
                .where(GlossaryConcept.glossary_id == gid)
            ).all()
            if t.term.startswith("gl615 term")
        }
    if statuses.get("gl615 term 1") != "admitted" or statuses.get("gl615 term 2") != "preferred":
        _fail(f"the status column must reach the source terms: {sorted(statuses.items())[:4]}")
    print("  term columns: status column applied through the batched pass OK")


def test_module_chunks_share_one_identity_map():
    """Review: a module chunk must not rescan the glossary for concept identity (the lower()
    pre-filter has no index: ~0.2 s per chunk at 100k entries made module imports 3-6x slower).
    The map is loaded once per import; a re-import still reuses every concept (no duplicates)."""
    gid = _module("glc615-idmap")
    first = _csv(range(40), prefix="Karte")
    with _Knobs(5):
        _import(None, gid, first)
    base = _concepts(gid)
    scans = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        flat = " ".join(statement.split()).lower()
        if "lower(glossaryentry.source_term)" in flat:
            scans.append(1)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        with _Knobs(5):
            res = _import(None, gid, _csv(range(40), prefix="Tafel"))
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    if res.get("write_chunks", 0) < 8:
        _fail(f"expected a multi-chunk module re-import: {res}")
    if scans:
        _fail(f"a chunk re-scanned the glossary by lower(source_term) ({len(scans)} scans)")
    if _concepts(gid) != base:
        _fail(f"re-import changed the concept count: {base} -> {_concepts(gid)}")
    print("  identity map: loaded once, concepts reused across chunks OK")


def test_identity_map_follows_a_concept_deleted_and_recreated():
    """Gate: the identity map is read before the first chunk. When another writer deletes a source
    surface's concept and creates the surface again (a NEW concept) while the import runs, a later
    chunk must link to the live concept, not mint a second one for the same surface."""
    fr = "fr-FR"
    gid = _module("glc615-idmap-recreate")
    with Session(engine) as s:
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}", f"{SRC}:{fr}"])
    with Session(engine) as s:
        gm.import_bytes(s, gid, _csv(range(300), prefix="Terme"), SRC, fr, filename="fr.csv")
    surface = "gl615 term 250"
    fired = []

    def sleeper(_s):
        if fired:
            return
        fired.append(1)
        with Session(engine) as other:  # chunk 1 (terms 0..99) is committed
            old = other.exec(
                select(GlossaryEntry).where(
                    GlossaryEntry.glossary_id == gid,
                    GlossaryEntry.source_term == surface,
                    GlossaryEntry.target_locale == fr,
                )
            ).one()
            gm.remove_term(other, gid, old.id)
        with Session(engine) as other:
            gm.add_term(other, gid, surface, "Terme neuf", SRC, fr)

    with _Knobs(100, pause=0.001, sleeper=sleeper), Session(engine) as s:
        gm.import_bytes(s, gid, _csv(range(300)), SRC, TGT, filename="de.csv")
    if not fired:
        _fail("the delete/recreate hook never ran")
    with Session(engine) as s:
        cids = set(
            s.exec(
                select(GlossaryEntry.concept_id).where(
                    GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == surface
                )
            ).all()
        )
    if len(cids) != 1 or None in cids:
        _fail(f"a recreated source surface got a second concept from the import: {cids}")
    if _concepts(gid) != 300:
        _fail(f"one concept per source surface expected, got {_concepts(gid)}")
    print("  identity map: a concept deleted and recreated meanwhile is linked, not duplicated OK")


def _run(test):
    """Run one test; on an unexpected exception name the test and the failing frame, compactly."""
    try:
        test()
    except SystemExit:
        raise
    except Exception as exc:
        import traceback

        frames = traceback.extract_tb(exc.__traceback__)[-3:]
        where = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in frames)
        print(f"FAIL in {test.__name__}: {type(exc).__name__}: {str(exc)[:200]} @ {where}")
        sys.exit(1)


if __name__ == "__main__":
    for _test in (
        test_chunk_bound_every_flavour,
        test_lock_free_between_chunks,
        test_chunks_shrink_when_slow,
        test_failure_halfway_then_retry,
        test_partial_failure_reverts_approval,
        test_edit_or_delete_during_import_is_reread_and_counted,
        test_term_created_by_another_writer_is_updated_not_duplicated,
        test_senses_survive_chunking,
        test_term_columns_pass_is_batched,
        test_module_chunks_share_one_identity_map,
        test_identity_map_follows_a_concept_deleted_and_recreated,
        test_surfaces_carry_chunks_and_deletes,
        test_no_internals_leak_on_any_surface,
    ):
        _run(_test)
    print("GLOSSARY CHUNKS 615 OK")
