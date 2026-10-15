"""Issue #584: TMX and glossary CSV imports hold the SQLite write lock for seconds, not minutes.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys, zero network.

Before this change `tm.import_tmx` and `glossary_hub._apply_rows` wrote while they parsed,
so SQLite's single WRITER lock was held from the first batch flush to the final commit: a
30000-unit TMX held it about 17 s, a 50000-row glossary CSV about 15 s, and every other
writer on the install waited out the 5 s busy_timeout and failed "database is locked".
Now both are two-phase: parse + validate + every read first (no writes), then ONE short
bulk write transaction. The imports stay all-or-nothing.

Covers:
  tmx-stall       -- while a 30000-unit TMX imports, a second thread on its own raw SQLite
                     connection tries a tiny write every 100 ms (with a long busy timeout, so
                     it WAITS instead of failing); the longest it waited must stay under the
                     busy timeout (pre-change: 15 to 17 s on the gate box). Also, one write
                     from a second connection LATE IN THE PARSE must succeed at once (the
                     deterministic check: it failed pre-change on any hardware).
  csv-stall / tbx-stall / module-csv-stall -- the same for a 30000-row glossary CSV, a
                     30000-concept TBX and a 5000-row MODULE glossary CSV (parse-lock-free only: its
                     concept reconcile still runs inside the write phase).
  tmx-atomic      -- a file whose LAST unit is malformed leaves zero rows; and a failure
                     injected in the MIDDLE of the write phase (after the bulk INSERTs,
                     at the UPDATE) rolls the INSERTs back too.
  csv-atomic      -- the same two shapes for the glossary CSV (a row cap breach as the
                     "refused after parsing" case, a forced UPDATE failure for the write).
  scope-changed   -- a row another connection commits between phase 1 and phase 2 makes the
                     import re-read once instead of inserting a duplicate row.

Final line: "IMPORT LOCK 584 OK".
"""

import os
import sqlite3
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_importlock584_")

from sqlalchemy import event, func
from sqlmodel import Session, select

from app import config, db
from app.db import engine, init_db
from app.models import GlossaryEntry, Project, SegmentTM
from app.services import glossary_hub, import_lock, tm

SRC, TGT = "en-US", "fr-FR"
N = 30000
N_MODULE = 5000


def _project(s, name):
    p = Project(name=name, source_locale=SRC)
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def _tu(src, tgt):
    return (
        f'<tu><tuv xml:lang="{SRC}"><seg>{src}</seg></tuv>'
        f'<tuv xml:lang="{TGT}"><seg>{tgt}</seg></tuv></tu>'
    )


def _tmx(units, closed=True):
    head = (
        f'<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="{SRC}"/><body>'
    )
    return (head + "".join(units) + ("</body></tmx>" if closed else "")).encode("utf-8")


def _csv(rows):
    return ("source_term,target_term\n" + "".join(f"{a},{b}\n" for a, b in rows)).encode("utf-8")


def _count(model, **where):
    with Session(engine) as s:
        q = select(func.count()).select_from(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return s.exec(q).one()


class _Prober:
    """A second writer on its OWN raw sqlite3 connection: tries a one-row write every
    100 ms and records the longest a write waited. busy_timeout is long on purpose, so a
    held write lock shows up as a LONG WAIT here, not as an early failure."""

    def __init__(self):
        self.path = engine.url.database
        con = sqlite3.connect(self.path, timeout=120)
        con.execute("CREATE TABLE IF NOT EXISTS lock_probe_584 (t REAL)")
        con.commit()
        con.close()
        self.waits: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        con = sqlite3.connect(self.path, timeout=120, isolation_level=None)
        con.execute("PRAGMA busy_timeout=120000")
        while not self._stop.is_set():
            t0 = time.monotonic()
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO lock_probe_584 VALUES (?)", (t0,))
            con.execute("COMMIT")
            self.waits.append(time.monotonic() - t0)
            self._stop.wait(0.1)
        con.close()

    def __enter__(self):
        self._thread.start()
        time.sleep(0.3)  # let it get a few unobstructed writes in first
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=150)
        return False

    @property
    def longest(self) -> float:
        return max(self.waits) if self.waits else 0.0


def _write_now() -> bool:
    """Can ANOTHER connection take the write lock right now? A 0.2 s busy timeout, so a lock
    held by the importer shows up as False instead of a long wait."""
    con = sqlite3.connect(engine.url.database, timeout=0.2, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=200")
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TABLE IF NOT EXISTS lock_probe_584 (t REAL)")
        con.execute("INSERT INTO lock_probe_584 VALUES (0)")
        con.execute("COMMIT")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


class _ParseProbe:
    """Tries one write from a second connection LATE IN THE PARSE (row `at` of the file).
    Deterministic, unlike the timing probe: the pre-change importers had been flushing
    rows inside an open write transaction for a long time by then, so this write failed
    ("database is locked"); a read-only parse phase lets it through at once."""

    def __init__(self, at):
        self.at = at
        self.results: list[bool] = []

    def wrap(self, rows):
        for i, row in enumerate(rows, 1):
            if i == self.at:
                self.results.append(_write_now())
            yield row

    def assert_free(self, label):
        assert self.results, f"{label}: the parse probe never ran (hook point changed?)"
        assert all(self.results), (
            f"{label}: another writer could not write while the file was still being parsed, "
            "i.e. the import already held the SQLite write lock"
        )


def _assert_short_lock(label, probe, elapsed, rows):
    """The timing bound: the longest a concurrent writer waited during the whole import
    must stay under db.BUSY_TIMEOUT_MS, which is when a normal writer starts failing with
    "database is locked" (pre-change: 15 to 17 s at this size on the gate box; a dedicated LLM
    server is
    about ten times faster than that, hence the deterministic parse probe above, which is
    the real discriminator on fast hardware)."""
    assert len(probe.waits) >= 3, f"{label}: the probe only got {len(probe.waits)} writes in"
    busy_s = db.BUSY_TIMEOUT_MS / 1000
    assert probe.longest < busy_s, (
        f"{label}: a concurrent writer waited {probe.longest:.1f} s during a {rows}-row import "
        f"({elapsed:.1f} s total); limit {busy_s:.0f} s (db.BUSY_TIMEOUT_MS)"
    )
    print(
        f"  {label}: {rows} rows imported in {elapsed:.1f} s; longest concurrent writer wait "
        f"{probe.longest:.2f} s over {len(probe.waits)} writes, and a write made late in the "
        f"parse went through OK"
    )


def _tbx(n):
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<tbx style="dca" type="TBX-Core" xml:lang="en">\n<text><body>'
    ]
    parts.extend(
        f'<conceptEntry id="c{i}">'
        f'<langSec xml:lang="{SRC}"><termSec><term>Row {i} pad pad pad</term></termSec></langSec>'
        f'<langSec xml:lang="{TGT}"><termSec><term>Fila {i} pad pad pad</term></termSec></langSec>'
        f"</conceptEntry>"
        for i in range(n)
    )
    parts.append("</body></text></tbx>")
    return "".join(parts).encode("utf-8")


def test_tmx_import_does_not_stall_other_writers():
    from app.services import seed_caps

    with Session(engine) as s:
        p = _project(s, "TmxStall")
        data = _tmx([_tu(f"Stall unit {i} pad pad", f"Unite {i} pad pad") for i in range(N)])
        pp = _ParseProbe(N * 5 // 6)
        real_check = seed_caps.check_import_rows

        def _check(n, *, what="This import"):
            if what == "This TMX file" and n == pp.at:
                pp.results.append(_write_now())
            return real_check(n, what=what)

        seed_caps.check_import_rows = _check
        try:
            with _Prober() as probe:
                t0 = time.monotonic()
                res = tm.import_tmx(s, p.id, data, SRC, TGT)
                elapsed = time.monotonic() - t0
        finally:
            seed_caps.check_import_rows = real_check
        assert res["imported"] == N, res
    pp.assert_free("tmx-stall")
    _assert_short_lock("tmx-stall", probe, elapsed, N)


def test_glossary_csv_import_does_not_stall_other_writers():
    with Session(engine) as s:
        p = _project(s, "CsvStall")
        data = _csv([(f"term {i} pad pad", f"terme {i} pad pad") for i in range(N)])
        pp = _ParseProbe(N * 5 // 6)
        real_rows = glossary_hub._csv_rows
        glossary_hub._csv_rows = lambda d: pp.wrap(real_rows(d))
        try:
            with _Prober() as probe:
                t0 = time.monotonic()
                res = glossary_hub.import_csv(s, p.id, data, SRC, TGT)
                elapsed = time.monotonic() - t0
        finally:
            glossary_hub._csv_rows = real_rows
        assert res["entries"] == N, res
    pp.assert_free("csv-stall")
    _assert_short_lock("csv-stall", probe, elapsed, N)


def test_glossary_tbx_import_does_not_stall_other_writers():
    from app.services import glossary_tbx

    with Session(engine) as s:
        p = _project(s, "TbxStall")
        pp = _ParseProbe(N * 5 // 6)
        real_parse = glossary_tbx.parse_rows

        def _parse(*a, **kw):
            rows, stats = real_parse(*a, **kw)
            return pp.wrap(rows), stats

        glossary_tbx.parse_rows = _parse
        try:
            with _Prober() as probe:
                t0 = time.monotonic()
                res = glossary_hub.import_bytes(s, p.id, _tbx(N), SRC, TGT, filename="t.tbx")
                elapsed = time.monotonic() - t0
        finally:
            glossary_tbx.parse_rows = real_parse
        assert res["entries"] == N, res
    pp.assert_free("tbx-stall")
    _assert_short_lock("tbx-stall", probe, elapsed, N)


def test_module_glossary_import_does_not_stall_other_writers():
    """A MODULE glossary import also reconciles the concept/term store inside the write
    phase; that is part of the lock window too."""
    from app.services import glossary_module as gm

    with Session(engine) as s:
        gid = gm.create(s, "Stall Module").id
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}"])
        data = _csv([(f"mterm {i} pad", f"mterme {i} pad") for i in range(N_MODULE)])
        pp = _ParseProbe(N_MODULE * 5 // 6)
        real_rows = glossary_hub._csv_rows
        glossary_hub._csv_rows = lambda d: pp.wrap(real_rows(d))
        try:
            with _Prober() as probe:
                t0 = time.monotonic()
                res = glossary_hub.import_csv(s, None, data, SRC, TGT, glossary_id=gid)
                elapsed = time.monotonic() - t0
        finally:
            glossary_hub._csv_rows = real_rows
        assert res["entries"] == N_MODULE, res
    pp.assert_free("module-csv-stall")
    # #597: the concept/term reconcile and the binding pass used to cost about 2.3 ms per entry
    # inside the lock (one INSERT per row on SQLite, three SELECTs per entry), so 5000 rows held
    # it 9 to 11 s. They are now batched (ids assigned as one executemany under the lock, one
    # query per table per batch), so the module import is held to the same bound as the others.
    _assert_short_lock("module-csv-stall", probe, elapsed, N_MODULE)


class _FailOn:
    """Make the first statement starting with `prefix` raise, via an engine event, so a
    failure lands in the MIDDLE of the write phase (after the bulk INSERTs)."""

    def __init__(self, prefix):
        self.prefix = prefix
        self.hits = 0

    def _hook(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(self.prefix):
            self.hits += 1
            raise RuntimeError("injected write-phase failure")

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._hook)
        return False


def test_tmx_import_stays_all_or_nothing():
    with Session(engine) as s:
        p = _project(s, "TmxAtomic")
        # (a) the LAST unit is malformed: nothing may be stored.
        bad = _tmx([_tu(f"A{i}", f"B{i}") for i in range(1200)], closed=False) + b"<tu><tuv"
        try:
            tm.import_tmx(s, p.id, bad, SRC, TGT)
        except Exception:
            pass
        else:
            raise AssertionError("a truncated TMX must raise")
        assert _count(SegmentTM, project_id=p.id) == 0
        # (b) a failure injected at the UPDATE, after the bulk INSERTs went out.
        seeded = tm.import_tmx(s, p.id, _tmx([_tu("Seed", "old")]), SRC, TGT)
        assert seeded["imported"] == 1, seeded
        doc = _tmx([_tu("Seed", "new")] + [_tu(f"New{i}", f"Nouveau{i}") for i in range(1200)])
        with _FailOn("UPDATE SEGMENTTM SET TARGET_TEXT") as inj:
            try:
                tm.import_tmx(s, p.id, doc, SRC, TGT)
            except RuntimeError:
                pass
            else:
                raise AssertionError("the injected failure must propagate")
        assert inj.hits >= 1, "the injection never fired (statement text changed?)"
        assert _count(SegmentTM, project_id=p.id) == 1, "the INSERTs of a failed import survived"
        with Session(engine) as s2:
            row = s2.exec(select(SegmentTM).where(SegmentTM.project_id == p.id)).one()
            assert row.target_text == "old", row.target_text
        # the session is still usable and a clean retry lands everything
        ok = tm.import_tmx(s, p.id, doc, SRC, TGT)
        assert ok["imported"] == 1200 and ok["updated"] == 1, ok
    print("  tmx-atomic: a malformed last unit and a mid-write failure both leave zero new rows OK")


def test_glossary_csv_import_stays_all_or_nothing():
    with Session(engine) as s:
        p = _project(s, "CsvAtomic")
        # (a) refused after the whole file was read: over the row cap.
        old_cap = config.MAX_DOCUMENT_ROWS
        config.MAX_DOCUMENT_ROWS = 1000
        try:
            try:
                glossary_hub.import_csv(
                    s, p.id, _csv([(f"t{i}", f"u{i}") for i in range(1001)]), SRC, TGT
                )
            except Exception:
                pass
            else:
                raise AssertionError("an over-cap CSV must be refused")
        finally:
            config.MAX_DOCUMENT_ROWS = old_cap
        assert _count(GlossaryEntry, project_id=p.id) == 0
        # (b) a failure injected at the UPDATE, after the bulk INSERT of new entries.
        glossary_hub.import_csv(s, p.id, _csv([("seed", "old")]), SRC, TGT)
        doc = _csv([("seed", "new")] + [(f"n{i}", f"m{i}") for i in range(1200)])
        with _FailOn("UPDATE GLOSSARYENTRY SET") as inj:
            try:
                glossary_hub.import_csv(s, p.id, doc, SRC, TGT)
            except RuntimeError:
                pass
            else:
                raise AssertionError("the injected failure must propagate")
        assert inj.hits >= 1, "the injection never fired (statement text changed?)"
        assert _count(GlossaryEntry, project_id=p.id) == 1, (
            "the INSERTs of a failed import survived"
        )
        res = glossary_hub.import_csv(s, p.id, doc, SRC, TGT)
        assert res["entries"] == 1200, res
    print("  csv-atomic: an over-cap file and a mid-write failure both leave zero new rows OK")


def test_row_committed_between_read_and_write_is_not_duplicated():
    """Phase 1 reads the scope, then (here, forced) another connection commits a row with
    the SAME source hash before phase 2 takes the write lock. The import must notice and
    re-read, not insert a second row for that hash."""
    with Session(engine) as s:
        p = _project(s, "ScopeChanged")
        real = import_lock.begin_write_phase
        fired = []

        def _racing(session, model):
            if not fired:
                fired.append(1)
                # a different connection adds the row the file is about to insert
                with Session(engine) as other:
                    tm.import_tmx(
                        other, p.id, _tmx([_tu("Racer", "from the other writer")]), SRC, TGT
                    )
            return real(session, model)

        import_lock.begin_write_phase = _racing
        try:
            res = tm.import_tmx(
                s, p.id, _tmx([_tu("Racer", "from the file"), _tu("Other", "Autre")]), SRC, TGT
            )
        finally:
            import_lock.begin_write_phase = real
        assert fired, "the race hook never ran"
        assert _count(SegmentTM, project_id=p.id) == 2, "a duplicate row for one hash was written"
        assert res["imported"] == 1 and res["updated"] == 1, res
    print("  scope-changed: a row committed between read and write is updated, not duplicated OK")


def test_lock_failure_leaves_no_open_savepoint_and_postgres_skips_the_noop():
    """Review: (a) when the write lock cannot be taken (another writer holds it past the
    busy timeout) the SAVEPOINT must not stay open on the caller's session; (b) the lock
    really IS taken by begin_write_phase on SQLite; (c) off SQLite the no-op UPDATE (whose
    `WHERE 0` is not a boolean on PostgreSQL) is never issued."""
    with Session(engine) as s:
        p = _project(s, "LockFail")
        holder = sqlite3.connect(engine.url.database, timeout=1, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            s.connection().exec_driver_sql("PRAGMA busy_timeout=100")
            try:
                tm.import_tmx(s, p.id, _tmx([_tu("Locked", "Verrouille")]), SRC, TGT)
            except Exception as exc:
                assert "locked" in str(exc).lower(), exc
            else:
                raise AssertionError("an import with the write lock held elsewhere must fail")
            assert not s.in_nested_transaction(), "a failed lock grab left the SAVEPOINT open"
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert _count(SegmentTM, project_id=p.id) == 0
        ok = tm.import_tmx(s, p.id, _tmx([_tu("Locked", "Verrouille")]), SRC, TGT)
        assert ok["imported"] == 1, ok
        # (b) the lock is really held between begin_write_phase and the commit
        sp = import_lock.begin_write_phase(s, SegmentTM)
        try:
            assert not _write_now(), "begin_write_phase did not take the SQLite write lock"
        finally:
            sp.rollback()
            s.rollback()  # a savepoint rollback keeps the outer transaction (and the lock)
        assert _write_now()
        # (c) not SQLite: no no-op UPDATE
        seen = []

        def _spy(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement)

        real = config.IS_SQLITE
        config.IS_SQLITE = False
        event.listen(engine, "before_cursor_execute", _spy)
        try:
            sp = import_lock.begin_write_phase(s, SegmentTM)
            sp.rollback()
        finally:
            event.remove(engine, "before_cursor_execute", _spy)
            config.IS_SQLITE = real
        assert not [x for x in seen if x.lstrip().upper().startswith("UPDATE")], seen
    print(
        "  lock-failure: savepoint closed on a lost lock; lock taken on SQLite; no-op skipped"
        " elsewhere OK"
    )


def test_commit_while_the_scope_loads_is_noticed():
    """Review: the glossary import must read PRAGMA data_version BEFORE it loads the
    existing entries; a commit by another connection that lands while the scope is loading
    (here: right after the load query ran) must make phase 2 re-check, not duplicate."""
    with Session(engine) as s:
        p = _project(s, "GlossaryLoadRace")
        fired = []

        def _hook(conn, cursor, statement, parameters, context, executemany):
            if (
                not fired
                and statement.lstrip().upper().startswith("SELECT GLOSSARYENTRY.SOURCE_TERM")
                and "GLOSSARYENTRY.META" in statement.upper()
            ):
                fired.append(1)
                with Session(engine) as other:
                    glossary_hub.import_csv(
                        other, p.id, _csv([("Racer", "other writer")]), SRC, TGT
                    )

        event.listen(engine, "after_cursor_execute", _hook)
        try:
            res = glossary_hub.import_csv(
                s, p.id, _csv([("Racer", "from the file"), ("Plain", "Simple")]), SRC, TGT
            )
        finally:
            event.remove(engine, "after_cursor_execute", _hook)
        assert fired, "the load-race hook never ran (statement text changed?)"
        assert _count(GlossaryEntry, project_id=p.id) == 2, "a duplicate glossary entry was written"
        with Session(engine) as s2:
            row = s2.exec(
                select(GlossaryEntry).where(
                    GlossaryEntry.project_id == p.id, GlossaryEntry.source_term == "Racer"
                )
            ).one()
            assert row.target_term == "from the file", row.target_term
        assert res["entries"] == 1, res  # only "Plain" is new; "Racer" was updated
    print("  load-race: a commit landing while the scope loads is noticed, no duplicate entry OK")


def test_sense_added_between_read_and_write_is_not_duplicated():
    """Review: senses have no unique key, so a sense another connection adds between the
    import's read and write must be re-read (updated), not inserted a second time."""
    from app.models import GlossarySense

    with Session(engine) as s:
        p = _project(s, "SenseRace")
        glossary_hub.import_csv(s, p.id, _csv([("Bank", "Banque")]), SRC, TGT)
        header = "source_term,target_term,sense_label,context_cue\n"
        mine = (header + "Bank,rive,finance,money\n").encode("utf-8")
        theirs = (header + "Bank,banque,finance,money\n").encode("utf-8")
        real = import_lock.begin_write_phase
        fired = []

        def _racing(session, model):
            if not fired:
                fired.append(1)
                with Session(engine) as other:
                    glossary_hub.import_csv(other, p.id, theirs, SRC, TGT)
            return real(session, model)

        import_lock.begin_write_phase = _racing
        try:
            glossary_hub.import_csv(s, p.id, mine, SRC, TGT)
        finally:
            import_lock.begin_write_phase = real
        assert fired
        with Session(engine) as s2:
            entry = s2.exec(
                select(GlossaryEntry).where(
                    GlossaryEntry.project_id == p.id, GlossaryEntry.source_term == "Bank"
                )
            ).one()
            senses = s2.exec(
                select(GlossarySense).where(GlossarySense.glossary_entry_id == entry.id)
            ).all()
        assert len(senses) == 1, f"{len(senses)} sense rows for one label"
        assert senses[0].target_term == "rive", senses[0].target_term
    print("  sense-race: a sense added between read and write is updated, not duplicated OK")


def test_module_reconcile_failure_rolls_back_when_no_entry_changed():
    """Gate: a MODULE glossary import whose file carries no usable row writes no entry, but
    its concept reconcile can still write. That reconcile must stay inside the import's
    savepoint, so a failure in it leaves nothing behind for a later commit to persist."""
    from app.services import glossary_concepts
    from app.services import glossary_module as gm

    with Session(engine) as s:
        gid = gm.create(s, "Reconcile Fail Module").id
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}"])
        real = glossary_concepts.reconcile_glossary_from_entries

        def _boom(session, glossary_id):
            session.add(
                GlossaryEntry(
                    glossary_id=glossary_id,
                    source_locale=SRC,
                    target_locale=TGT,
                    source_term="written by a failed reconcile",
                    target_term="x",
                )
            )
            session.flush()
            raise RuntimeError("injected reconcile failure")

        glossary_concepts.reconcile_glossary_from_entries = _boom
        try:
            try:
                glossary_hub.import_csv(
                    s, None, _csv([(" ", "blank source")]), SRC, TGT, glossary_id=gid
                )
            except RuntimeError:
                pass
            else:
                raise AssertionError("the injected reconcile failure must propagate")
        finally:
            glossary_concepts.reconcile_glossary_from_entries = real
        s.commit()  # a caller that goes on and commits must not persist the failed import
        assert _count(GlossaryEntry, glossary_id=gid) == 0, "a failed reconcile's write survived"
    print("  module-reconcile-atomic: a failed reconcile on an entry-free import leaves nothing OK")


if __name__ == "__main__":
    init_db()
    test_module_reconcile_failure_rolls_back_when_no_entry_changed()
    test_row_committed_between_read_and_write_is_not_duplicated()
    test_tmx_import_stays_all_or_nothing()
    test_glossary_csv_import_stays_all_or_nothing()
    test_lock_failure_leaves_no_open_savepoint_and_postgres_skips_the_noop()
    test_commit_while_the_scope_loads_is_noticed()
    test_sense_added_between_read_and_write_is_not_duplicated()
    test_tmx_import_does_not_stall_other_writers()
    test_glossary_csv_import_does_not_stall_other_writers()
    test_glossary_tbx_import_does_not_stall_other_writers()
    test_module_glossary_import_does_not_stall_other_writers()
    print("IMPORT LOCK 584 OK")
