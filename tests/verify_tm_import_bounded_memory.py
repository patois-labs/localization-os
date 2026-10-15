"""tm.import_tmx BOUNDED-MEMORY verification (periodic flush + SAVEPOINT) -- hermetic.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys, zero network.

Context: the streaming rewrite (verify_tm_import_streaming.py) bounded the XML
PARSER's memory but not the SESSION's. Every processed <tu> left a live SegmentTM
in the identity map + the flush plan until one final commit, and the idempotency
map held a preloaded ORM instance per existing row, so heap grew with the FILE,
not with the batch. MEASURED on the pre-change code (28.3 MB / 150k-unit TMX):
  * fresh import  -- 732.5 MB peak traced heap = 25.9x the file
  * re-import     -- 348.0 MB peak            = 12.3x the file
i.e. an OOM on a 1 GB machine at a size the (now 100 MiB) upload cap admits.
After the change, same fixture, same measurement: 49.7 MB (1.76x) and 78.2 MB
(2.77x). import_tmx now flushes + EXPUNGES every tm.TMX_IMPORT_FLUSH_BATCH units
inside a SAVEPOINT, and keys idempotency off primitives (hash -> (id, target)).

Covers:
  dense-memory   -- a dense many-small-units TMX generated in the throwaway dir
                    imports with peak traced heap under 5x the file size. On the
                    pre-change code this same test measured 25.9x (re-confirmed by
                    mutation on 2026-07-27: with the periodic flush disabled this
                    exact fixture measures 26.2x and the 5x bound FAILS, while the
                    shipped path measures 2.0x and wall clock is unchanged), so
                    the bound cannot pass a regression back to accumulate-then-
                    commit. The peak bound is the ONLY assertion here that detects
                    that mutation -- the identity/dirty-set checks below are a
                    complementary end-state claim, and the final _flush_batch()
                    satisfies them even when the periodic flush is gone.
  flush-failure  -- the OTHER atomicity tests all fail through an XML ParseError,
                    where the savepoint is still ACTIVE. A DB-level failure inside
                    session.flush() is a different SQLAlchemy path (the failed
                    flush DEACTIVATES the enclosing savepoint first), and the
                    except block must still leave the caller's session usable.
  atomicity      -- a document that goes malformed AFTER several FULL batches were
                    already flushed to the DB persists NOTHING (checked from a
                    second, independent session, so it is a DB-level claim, not a
                    session-cache one), and leaves the caller's session usable for
                    a later unit of work. A per-object undo could not do this: the
                    flushed rows were already written. ROLLBACK TO SAVEPOINT is.
  caller-pending -- work the CALLER left pending on the same session survives a
                    failed import (it is flushed into the OUTER transaction when
                    the savepoint opens, exactly as autoflush already flushed it
                    on import_tmx's first SELECT) and still commits afterwards.
  straddling-dup -- an in-file duplicate whose first occurrence is in an EARLIER
                    batch (already flushed AND expunged) still resolves as an
                    update of that same row: one row, last target, `updated`
                    counter -- never a second INSERT and never a stale-object
                    write. Asserted across many batch sizes AND at both
                    same-target (skipped) and changed-target (updated) shapes.
  batch-invariance -- counts and stored rows are IDENTICAL for every batch size
                    from 1 to larger-than-the-file, including the shipped 500, on
                    a fixture carrying duplicates, refusals, locale fallback and
                    meta round-trip. (The counts-vs-pre-change-oracle proof lives
                    in verify_tm_import_streaming.py, which owns the full-DOM
                    reference implementation; it now runs at several batch sizes.)

Final line: "TM IMPORT BOUNDED MEMORY OK".
"""

import gc
import os
import sys
import tracemalloc

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

_TMP = init_env("loctms_tmbounded_")

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Project, SegmentTM
from app.services import tm

SRC, TGT = "en-US", "fr-FR"

HEDGE = (
    "I'm ready to translate. However, I don't see any source text. "
    "Please provide the actual source text you'd like me to translate."
)
from app.services import refusal

assert refusal.is_refusal(HEDGE), "test fixture must itself be a detected refusal"


def _counters(res):
    """The four IMPORT COUNTERS of a tm.import_tmx result.

    import_tmx additionally returns the EFFECTIVE locales it stored under plus
    "locales_normalized" (QA BUG-03, 2026-07-28) -- additive keys this suite's
    exact-dict assertions predate. Those are pinned in verify_tm_import_locales.py;
    here we compare counters only, which is what these tests are actually about."""
    return {k: res[k] for k in ("imported", "skipped", "updated", "skipped_refusal")}


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


def _doc(units, closed=True):
    head = (
        f'<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="{SRC}"/><body>'
    )
    return (head + "".join(units) + ("</body></tmx>" if closed else "")).encode("utf-8")


def _rows(session, project_id):
    rows = session.exec(select(SegmentTM).where(SegmentTM.project_id == project_id)).all()
    return sorted((r.source_text, r.target_text) for r in rows)


# =============================================================================
def test_dense_file_peak_heap_is_bounded():
    """A dense (many small units) TMX is the worst shape for per-row session
    state: the pre-change code paid ~5 KB of live Python heap per <tu> no matter
    how small the unit was, so heap tracked the UNIT COUNT while the upload cap
    only bounds the BYTE count. Generated to a file in the throwaway data dir and
    removed right after (house rule: no test debris).

    The bound is on tracemalloc's peak, which counts only allocations made after
    start() -- the file bytes are read BEFORE, so this is the import's own heap,
    measured against the file size. 5x is deliberately loose: post-change this
    fixture measures ~1.8x and pre-change ~26x, so nothing but a real regression
    back to accumulate-until-commit can trip it."""
    n, pad = 40000, "x" * 40
    units = [
        f'<tu><tuv xml:lang="{SRC}"><seg>Row {i} {pad}</seg></tuv>'
        f'<tuv xml:lang="{TGT}"><seg>Fila {i} {pad}</seg></tuv></tu>'
        for i in range(n)
    ]
    blob = _doc(units)
    del units
    path = os.path.join(_TMP, "dense_units.tmx")
    with open(path, "wb") as fh:
        fh.write(blob)
    del blob
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        size = len(data)
        # dense enough that the unit count, not the byte count, is the pressure
        assert size / n < 400, f"fixture is not dense: {size / n:.0f} bytes/unit"
        with Session(engine) as s:
            p = _project(s, "DenseUnits")
            gc.collect()
            tracemalloc.start()
            res = tm.import_tmx(s, p.id, data, SRC, TGT)
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            assert _counters(res) == {
                "imported": n,
                "skipped": 0,
                "updated": 0,
                "skipped_refusal": 0,
            }, res
            count = s.exec(
                select(func.count()).select_from(SegmentTM).where(SegmentTM.project_id == p.id)
            ).one()
            assert count == n, count
            # END-STATE hygiene, not a second proof of the bound: no SegmentTM
            # identity survives the import, so nothing this call added can be
            # picked back up by a later autoflush. (The count query above loads
            # no SegmentTM.) NB this holds even with the periodic flush disabled
            # -- the trailing _flush_batch() expunges either way -- so the peak
            # assertion below is what actually pins the memory behaviour.
            held = [o for o in s.identity_map.values() if isinstance(o, SegmentTM)]
            assert held == [], f"{len(held)} SegmentTM rows still held after import"
            assert not s.new and not s.dirty, (len(s.new), len(s.dirty))
        assert peak < 5 * size, (
            f"import of a {size / 1e6:.1f} MB / {n}-unit TMX peaked at "
            f"{peak / 1e6:.1f} MB of Python heap ({peak / size:.1f}x the file)"
        )
    finally:
        os.remove(path)
    print(
        f"  bounded memory: {size / 1e6:.1f} MB / {n} dense units imported in "
        f"{peak / 1e6:.1f} MB peak Python heap ({peak / size:.2f}x the file, bound 5x; "
        f"pre-change this shape was ~26x), session left holding no rows, "
        f"temp file removed OK"
    )


def test_failure_after_flushed_batches_persists_nothing():
    """The atomicity claim, at the size where it actually bites: the document goes
    malformed only AFTER several FULL batches have been flushed to the DB at the
    SHIPPED batch size. Pre-change those rows were merely pending in the session
    and a per-object expunge could undo them; now they are written, and only
    ROLLBACK TO SAVEPOINT can take them back."""
    n = tm.TMX_IMPORT_FLUSH_BATCH * 3 + 7
    good = [_tu(f"Unit {i}", f"Unite {i}") for i in range(n)]
    truncated = _doc(good, closed=False) + f'<tu><tuv xml:lang="{SRC}"><seg>tail'.encode()
    with Session(engine) as s:
        p = _project(s, "FlushedThenBroken")
        # seed a row so the UPDATE branch is exercised by the failing import too
        seeded = tm.import_tmx(s, p.id, _doc([_tu("Unit 0", "SEEDED")]), SRC, TGT)
        assert seeded["imported"] == 1, seeded
        raised = False
        try:
            tm.import_tmx(s, p.id, truncated, SRC, TGT)
        except Exception:
            raised = True
        assert raised, "a truncated document must still raise"
        # (1) same session: a follow-up query autoflushes anything left pending
        assert _rows(s, p.id) == [("Unit 0", "SEEDED")], _rows(s, p.id)
        # (2) the session is still usable for a later unit of work
        s.commit()
        ok = tm.import_tmx(s, p.id, _doc([_tu("After", "Apres")]), SRC, TGT)
        assert _counters(ok) == {"imported": 1, "skipped": 0, "updated": 0, "skipped_refusal": 0}, (
            ok
        )
        s.commit()
    # (3) the DB-level claim, from an independent session
    with Session(engine) as s2:
        p2 = s2.exec(select(Project).where(Project.name == "FlushedThenBroken")).first()
        assert _rows(s2, p2.id) == [("After", "Apres"), ("Unit 0", "SEEDED")], _rows(s2, p2.id)
    print(
        f"  bounded memory: a failure after {n // tm.TMX_IMPORT_FLUSH_BATCH} "
        f"flushed batches ({n} units) persists NOTHING (checked from a second "
        f"session) and leaves the caller's session usable OK"
    )


def test_flush_time_db_failure_leaves_the_session_usable():
    """Every other atomicity test drives the failure through an XML ParseError,
    raised while the SAVEPOINT is still ACTIVE -- so `savepoint.rollback()` is a
    plain undo. A DB-level failure inside `session.flush()` is a materially
    different SQLAlchemy path: the failed flush rolls back its own subtransaction
    with _capture_exception and DEACTIVATES the enclosing savepoint, so the except
    block is calling rollback() on an already-deactivated transaction. That is the
    path a constraint violation, a disk-full or a concurrent-delete StaleDataError
    takes at the 100 MiB cap, and the whole point of the except block is that the
    caller's session comes back usable -- so it gets its own proof.

    Induced with a project_id that does not exist: SQLite enforces FKs here
    (db.py sets PRAGMA foreign_keys=ON per connection), so the INSERT fails at
    FLUSH time, after the savepoint is open, not at parse time."""
    ghost = 10_000_000
    with Session(engine) as s:
        p = _project(s, "FlushFailHost")
        s.add(Project(name="FlushFailCallerWork", source_locale=SRC))  # caller-owned
        raised = None
        try:
            tm.import_tmx(s, ghost, _doc([_tu("X", "Y")]), SRC, TGT)
        except Exception as exc:
            raised = exc
        assert isinstance(raised, IntegrityError), raised
        # the session is usable again: query, then a REAL import, then commit
        assert _rows(s, p.id) == [], _rows(s, p.id)
        ok = tm.import_tmx(s, p.id, _doc([_tu("After", "Apres")]), SRC, TGT)
        assert _counters(ok) == {"imported": 1, "skipped": 0, "updated": 0, "skipped_refusal": 0}, (
            ok
        )
        s.commit()
    with Session(engine) as s2:
        assert (
            s2.exec(
                select(func.count()).select_from(SegmentTM).where(SegmentTM.project_id == ghost)
            ).one()
            == 0
        )
        names = [r.name for r in s2.exec(select(Project)).all()]
        assert "FlushFailCallerWork" in names, names
        p2 = s2.exec(select(Project).where(Project.name == "FlushFailHost")).first()
        assert _rows(s2, p2.id) == [("After", "Apres")], _rows(s2, p2.id)
    print(
        "  bounded memory: a FLUSH-time DB failure (FK violation) rolls the "
        "savepoint back from its deactivated state, persists nothing, keeps "
        "caller-owned work, and leaves the session usable for real work OK"
    )


def test_caller_pending_work_survives_a_failed_import():
    """import_tmx opens its savepoint AFTER the session's pending state is flushed
    (SessionTransaction._take_snapshot does that flush; autoflush on import_tmx's
    first SELECT already did the same before this change), so caller-owned pending
    work sits in the OUTER transaction and a savepoint rollback cannot discard it.
    Documented here because the previous exception path deliberately avoided
    session.rollback() for exactly this reason."""
    with Session(engine) as s:
        p = _project(s, "CallerPendingHost")
        pending = Project(name="CallerPendingWork", source_locale=SRC)
        s.add(pending)  # caller-owned, still pending when import_tmx is called
        broken = _doc([_tu("A", "A-FR"), _tu("B", "B-FR")], closed=False) + b"<tu><tuv"
        try:  # noqa: SIM105
            tm.import_tmx(s, p.id, broken, SRC, TGT)
        except Exception:
            pass
        assert _rows(s, p.id) == [], "the failed import must have written nothing"
        s.commit()
    with Session(engine) as s2:
        names = [r.name for r in s2.exec(select(Project)).all()]
        assert "CallerPendingWork" in names, names
        p2 = s2.exec(select(Project).where(Project.name == "CallerPendingHost")).first()
        assert _rows(s2, p2.id) == [], _rows(s2, p2.id)
    print(
        "  bounded memory: caller-owned pending work survives a failed import "
        "(savepoint rollback does not reach the outer transaction) OK"
    )


def _straddle_doc(batch, second_target):
    """A duplicate source whose two occurrences are separated by more than one
    batch, so the first is flushed AND expunged before the second is read."""
    units = [_tu("DUPLICATE", "first target")]
    units += [_tu(f"Filler {i}", f"Bourre {i}") for i in range(batch * 2 + 1)]
    units.append(_tu("DUPLICATE", second_target))
    return _doc(units), len(units)


def test_in_file_duplicate_straddling_a_flush_boundary():
    """The dedupe map used to hold the live ORM object for a just-inserted row, so
    a repeated <tu> mutated that object. Now the row is flushed and expunged at the
    batch boundary, so the second occurrence must resolve against its ID -- as an
    UPDATE of the same row. If it did not, the file would produce TWO rows for one
    source_hash and every later exact-hash lookup would be ambiguous.

    Run at several batch sizes (including 1 and the shipped default) and for both
    duplicate shapes: a CHANGED target (updated) and an IDENTICAL one (skipped)."""
    original = tm.TMX_IMPORT_FLUSH_BATCH
    try:
        for batch in (1, 2, 3, 7, original):
            set_pieces("app.services.tm", "TMX_IMPORT_FLUSH_BATCH", batch)
            for label, second, exp_updated, exp_skipped, exp_target in (
                ("changed", "second target", 1, 0, "second target"),
                ("identical", "first target", 0, 1, "first target"),
            ):
                data, n_units = _straddle_doc(batch, second)
                with Session(engine) as s:
                    p = _project(s, f"Straddle-{batch}-{label}")
                    res = tm.import_tmx(s, p.id, data, SRC, TGT)
                    # Counters only: import_tmx also returns the EFFECTIVE locales +
                    # "locales_normalized" (QA BUG-03, additive).
                    assert {
                        k: res[k] for k in ("imported", "skipped", "updated", "skipped_refusal")
                    } == {
                        "imported": n_units - 1,
                        "skipped": exp_skipped,
                        "updated": exp_updated,
                        "skipped_refusal": 0,
                    }, (batch, label, res)
                    dup = s.exec(
                        select(SegmentTM).where(
                            SegmentTM.project_id == p.id, SegmentTM.source_text == "DUPLICATE"
                        )
                    ).all()
                    assert len(dup) == 1, (batch, label, len(dup))
                    assert dup[0].target_text == exp_target, (batch, label, dup[0].target_text)
                    total = s.exec(
                        select(func.count())
                        .select_from(SegmentTM)
                        .where(SegmentTM.project_id == p.id)
                    ).one()
                    assert total == n_units - 1, (batch, label, total)
    finally:
        set_pieces("app.services.tm", "TMX_IMPORT_FLUSH_BATCH", original)
    print(
        "  bounded memory: an in-file duplicate straddling a flush boundary "
        "updates the SAME row (one row, last target) at batch sizes "
        "1/2/3/7/default, for changed and identical targets OK"
    )


def _mixed_fixture():
    """Every branch at once: fallback locales, a missing pair, a refusal hedge, a
    meta-carrying unit, an in-file duplicate, and a plain unit -- repeated so the
    duplicate pair lands in different batches at different batch sizes."""
    units = [
        _tu("Hello world", "Bonjour le monde"),
        '<tu><tuv xml:lang="en"><seg>Base locale</seg></tuv>'
        '<tuv xml:lang="fr"><seg>Locale de base</seg></tuv></tu>',
        f'<tu><tuv xml:lang="{SRC}"><seg>Missing pair</seg></tuv></tu>',
        _tu("Hedge me", HEDGE),
        f'<tu><prop type="x-origin">connector:jira</prop>'
        f'<tuv xml:lang="{SRC}"><seg>Provenance</seg></tuv>'
        f'<tuv xml:lang="{TGT}"><seg>Provenance FR</seg></tuv></tu>',
    ]
    units += [_tu(f"Body {i}", f"Corps {i}") for i in range(12)]
    units.append(_tu("Hello world", "Salut le monde"))  # in-file duplicate
    units += [_tu(f"Tail {i}", f"Queue {i}") for i in range(5)]
    units.append(_tu("Body 3", "Corps 3"))  # identical duplicate
    return _doc(units)


def test_counts_and_rows_are_invariant_under_batch_size():
    """Batching must be INVISIBLE: the same file must produce the same counts and
    the same stored rows whether it is flushed once, per unit, or anywhere in
    between. (The equivalence to the PRE-batching implementation itself is proved
    in verify_tm_import_streaming.py against its full-DOM oracle.)"""
    data = _mixed_fixture()
    original = tm.TMX_IMPORT_FLUSH_BATCH
    baseline = None
    try:
        for batch in (1, 2, 3, 5, 17, original, 100000):
            set_pieces("app.services.tm", "TMX_IMPORT_FLUSH_BATCH", batch)
            with Session(engine) as s:
                p = _project(s, f"Invariant-{batch}")
                res = tm.import_tmx(s, p.id, data, SRC, TGT)
                rows = _rows(s, p.id)
                metas = sorted(
                    (r.source_text, tuple(sorted((r.meta or {}).items())))
                    for r in s.exec(select(SegmentTM).where(SegmentTM.project_id == p.id)).all()
                )
                # re-import at the same batch size is a pure no-op
                again = tm.import_tmx(s, p.id, data, SRC, TGT)
            got = (res, rows, metas, again)
            if baseline is None:
                baseline = got
                assert _counters(res) == {
                    "imported": 20,
                    "skipped": 1,
                    "updated": 1,
                    "skipped_refusal": 1,
                }, res
                # 24 units: 1 missing-pair (silent), 1 hedge (skipped_refusal),
                # 1 changed in-file dup (updated), 1 identical in-file dup
                # (skipped), 20 inserted.
                # Re-import: 20 rows already match, and the CHANGED in-file
                # duplicate oscillates (its two <tu>s disagree, so each one
                # updates the stored row again) -- real, pre-existing behavior,
                # asserted here only to pin it as batch-size-independent.
                assert _counters(again) == {
                    "imported": 0,
                    "skipped": 20,
                    "updated": 2,
                    "skipped_refusal": 1,
                }, again
                assert ("Provenance", (("origin", "connector:jira"),)) in metas, metas
            else:
                assert got == baseline, (batch, got, baseline)
    finally:
        set_pieces("app.services.tm", "TMX_IMPORT_FLUSH_BATCH", original)
    print(
        "  bounded memory: counts, stored rows, meta round-trip and re-import "
        "no-op are identical at batch sizes 1/2/3/5/17/default/whole-file OK"
    )


if __name__ == "__main__":
    init_db()
    test_in_file_duplicate_straddling_a_flush_boundary()
    test_counts_and_rows_are_invariant_under_batch_size()
    test_caller_pending_work_survives_a_failed_import()
    test_flush_time_db_failure_leaves_the_session_usable()
    test_failure_after_flushed_batches_persists_nothing()
    test_dense_file_peak_heap_is_bounded()
    print("TM IMPORT BOUNDED MEMORY OK")
