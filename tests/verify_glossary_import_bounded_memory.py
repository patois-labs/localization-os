"""glossary._apply_rows / glossary_tbx.parse_rows BOUNDED-MEMORY verification
(periodic flush + SAVEPOINT, streamed TBX parse) -- hermetic.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys, zero network.

Context (chip task_67a4223c, 2026-07-29): the glossary bulk write path used to
hold ONE LIVE GlossaryEntry per DISTINCT term for the WHOLE import (a
``term -> ORM object`` dict that never shrank) and COMMIT once per brand-new
entry, while glossary_tbx.parse_rows built the WHOLE TBX row list before
returning anything. With the 100 MiB upload cap and the ~54 MB Microsoft
Terminology termbase (hundreds of thousands of terms) as a real input, that is
an OOM risk on a 1 GB machine, and a wall-time problem from committing a
whole transaction per new term.

Follows the tm.import_tmx pattern (commit cf060df / a95a402): parse_rows now
streams its rows as a generator; _apply_rows preloads pre-existing entries in
scope as PRIMITIVES (never a live GlossaryEntry), holds a brand-new term's ORM
object only until the next GLOSSARY_IMPORT_FLUSH_BATCH boundary (flushed, not
committed, then expunged), and queues further changes to an already-flushed
term as one bulk UPDATE per batch -- all inside ONE SAVEPOINT, released with a
single COMMIT at the end.

Covers:
  dense-memory (TBX + CSV) -- a dense many-distinct-terms file of each format,
                    generated in the throwaway dir and removed right after,
                    imports with peak traced heap under 5x the file size.
  mutation probe -- the SAME TBX fixture, imported with GLOSSARY_IMPORT_FLUSH_
                    BATCH set absurdly high (defeats the periodic flush, so
                    every brand-new term's ORM object stays live for the WHOLE
                    file -- the closest in-repo proxy to the pre-fix shape):
                    peak blows well past the 5x bound the shipped batch size
                    clears, proving the bound is actually pinned BY the
                    batching, not incidentally satisfied regardless of it.
  atomicity       -- a TBX that goes malformed AFTER several full batches have
                    already been flushed to the DB persists NOTHING (checked
                    from a second, independent session), and leaves the
                    caller's session usable for later work.
  straddling-dup  -- an in-file duplicate source_term whose two occurrences are
                    separated by more than one batch (so the first is flushed
                    AND expunged before the second is read) still resolves as
                    an UPDATE of the SAME row -- one row, last value wins --
                    across several batch sizes, for both CSV and TBX.
  batch-invariance-- counts and stored rows are IDENTICAL for a mixed CSV
                    fixture (senses, meta, dnt, an in-file duplicate) at
                    several batch sizes, including the shipped default.
  re-import       -- importing a SECOND file into an ALREADY-POPULATED project
                    updates existing (preloaded, primitives-only) entries
                    correctly -- target_term, dnt and meta all land, and a
                    query on the SAME session that just wrote them sees the new
                    values (the bulk `session.execute(sa.update(...))` path,
                    unexercised by any pre-existing suite, since every prior
                    glossary_tbx/glossary test only ever imports into a FRESH
                    project/glossary).
  counters        -- BUG-09 (QA campaign, 2026-07-29): reported import counts
                    did not add up against the rows actually in the file (a row
                    touching an already-known term vanished from every count).
                    Pins the fix's reconciling identity -- rows == entries +
                    updated + skipped_rows -- for CSV and TBX alike, that
                    import_csv's own narrow legacy return shape is unchanged,
                    and (review round 1) that the CSV arm still emits NO
                    "skipped" key, since every surface renders that key as "no
                    matching language pair" and blank CSV rows are not that.
  changed counters -- the approval-revert fix (2026-07-29): `changed` /
                    `unchanged` split the row-count `updated` into rows that
                    really moved a stored value and true no-ops, because
                    glossary_module.import_bytes reverts an APPROVED glossary on
                    `changed`. Pins that the split is BATCH-INVARIANT (the
                    mutated row straddles a flush boundary, so it is compared
                    through the primitives cache, not a live ORM object) and
                    that a re-imported sense row UPSERTS by (entry, sense_label)
                    instead of inserting a duplicate -- including a duplicate
                    sense whose two occurrences straddle a flush boundary.
  synonym concept -- review round 1: ONE conceptEntry listing several source
                    terms is the normal shape of a real termbase (MS/IATE), and
                    it emits one ROW PER TERM, so `concepts` can never be
                    reconciled term-for-term against entries+updated. Pins the
                    true relation (an inequality) so the old, false identity
                    cannot be re-asserted.

Final line: "GLOSSARY IMPORT BOUNDED MEMORY OK".
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

_TMP = init_env("loctms_glossbounded_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import GlossaryEntry, GlossarySense, Project
from app.services import glossary

SRC, TGT = "en-US", "es-ES"
_N = 0


def _project(name):
    global _N
    _N += 1
    with Session(engine) as s:
        p = Project(name=f"{name} {_N}", source_locale=SRC)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _terms(pid):
    with Session(engine) as s:
        rows = s.exec(select(GlossaryEntry).where(GlossaryEntry.project_id == pid)).all()
        return {e.source_term: (e.target_term, e.dnt, e.meta or {}) for e in rows}


def _tbx_doc(n, pad="x" * 40):
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<tbx style="dca" type="TBX-Core" xml:lang="en">\n<text><body>'
    ]
    parts.extend(
        f'<conceptEntry id="c{i}">'
        f'<langSec xml:lang="{SRC}"><termSec><term>Row {i} {pad}</term></termSec></langSec>'
        f'<langSec xml:lang="{TGT}"><termSec><term>Fila {i} {pad}</term></termSec></langSec>'
        f"</conceptEntry>"
        for i in range(n)
    )
    parts.append("</body></text></tbx>")
    return "".join(parts).encode("utf-8")


def _csv_doc(n, pad="x" * 40):
    lines = ["source_term,target_term,dnt,sense_label,context_cue"]
    lines.extend(f"Row {i} {pad},Fila {i} {pad},,," for i in range(n))
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _write_temp(name, data):
    path = os.path.join(_TMP, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _peak_import(path, importer, pid):
    """Read `path` back (so file-read allocation is OUTSIDE the traced window,
    mirroring verify_tm_import_bounded_memory.py), import it via `importer`,
    return (peak_bytes, file_size, result_dict)."""
    with open(path, "rb") as fh:
        data = fh.read()
    size = len(data)
    with Session(engine) as s:
        gc.collect()
        tracemalloc.start()
        res = importer(s, data)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    return peak, size, res


# =============================================================================
def test_dense_tbx_peak_heap_is_bounded():
    """A dense (many distinct single-term-pair concepts) TBX is the worst shape
    for per-term session state: the pre-batching code paid one live GlossaryEntry
    (plus a COMMIT) per distinct term regardless of how small the term was."""
    n = 12000
    path = _write_temp("dense.tbx", _tbx_doc(n))
    try:
        pid = _project("Dense TBX")
        peak, size, res = _peak_import(
            path,
            lambda s, data: glossary.import_bytes(s, pid, data, SRC, TGT, filename="dense.tbx"),
            pid,
        )
        assert res["entries"] == n and res["format"] == "tbx", res
        assert res["skipped"] == 0 and res["skipped_rows"] == 0, res
        with Session(engine) as s:
            count = len(s.exec(select(GlossaryEntry).where(GlossaryEntry.project_id == pid)).all())
        assert count == n, count
        assert peak < 5 * size, (
            f"import of a {size / 1e6:.1f} MB / {n}-term TBX peaked at "
            f"{peak / 1e6:.1f} MB of Python heap ({peak / size:.1f}x the file)"
        )
        print(
            f"  bounded memory (TBX): {size / 1e6:.1f} MB / {n} distinct terms "
            f"imported in {peak / 1e6:.1f} MB peak Python heap "
            f"({peak / size:.2f}x the file, bound 5x) OK"
        )
    finally:
        os.remove(path)


def test_dense_csv_peak_heap_is_bounded():
    """Same write path, CSV format. NOTE on the bound's size: CSV's byte
    footprint is a few dozen bytes/row versus a few hundred for TBX/TMX, so the
    SAME per-row Python overhead (a small state-dict entry, an ORM object while
    pending -- the part that batching actually bounds) is naturally a much
    LARGER multiple of a tiny CSV file than of a verbose XML one, even on the
    fully-batched path. This is not the same claim as the TBX test above (peak
    is not proportionally tighter just because the format is smaller) -- it is
    still a REAL bound: measured 30k distinct terms / ~3.2 MB peaking at ~10x
    the file (batched) versus a state-dict-per-term cost that is O(distinct
    terms) regardless of format (the "inherent to dedupe" floor tm.import_tmx's
    own docstring names for its primitives map). 20x is deliberately loose
    around that measurement; the mutation probe below is what actually proves
    batching -- not incidental file-size luck -- is what keeps this bounded."""
    n = 12000
    path = _write_temp("dense.csv", _csv_doc(n))
    try:
        pid = _project("Dense CSV")
        peak, size, res = _peak_import(
            path, lambda s, data: glossary.import_csv(s, pid, data, SRC, TGT), pid
        )
        assert res == {"entries": n, "senses": 0}, res
        with Session(engine) as s:
            count = len(s.exec(select(GlossaryEntry).where(GlossaryEntry.project_id == pid)).all())
        assert count == n, count
        assert peak < 20 * size, (
            f"import of a {size / 1e6:.1f} MB / {n}-term CSV peaked at "
            f"{peak / 1e6:.1f} MB of Python heap ({peak / size:.1f}x the file)"
        )
        print(
            f"  bounded memory (CSV): {size / 1e6:.1f} MB / {n} distinct terms "
            f"imported in {peak / 1e6:.1f} MB peak Python heap "
            f"({peak / size:.2f}x the file, bound 20x -- looser than TBX's "
            "because CSV bytes/row are small relative to the fixed per-term "
            "Python overhead; see the mutation probe for the real proof) OK"
        )
    finally:
        os.remove(path)


def _module_glossary():
    global _N
    _N += 1
    from app.services import glossary_module as gm

    with Session(engine) as s:
        g = gm.create(s, f"Bounded Module {_N}")
        gid = g.id
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}"])
    return gid


def test_dense_csv_module_glossary_peak_heap_is_bounded():
    """MODULE-glossary arm (gate round-1 M5): a MODULE CSV import triggers the concept/term
    dual-write reconcile — the exact path the project arms above skip (they import with
    glossary_id=None). The reconcile processes entries in GLOSSARY_IMPORT_FLUSH_BATCH-sized
    batches and expunges each, so live ORM state stays O(batch); an UNBOUNDED reconcile
    (one live concept+2 terms per distinct term for the whole file) would blow past this.
    Also asserts a re-import is IDEMPOTENT (no duplicate concepts, M4) and stays bounded."""
    from app.models import GlossaryConcept

    n = 3000
    path = _write_temp("dense_module.csv", _csv_doc(n))
    try:
        gid = _module_glossary()
        peak, size, res = _peak_import(
            path,
            lambda s, data: glossary.import_bytes(
                s, None, data, SRC, TGT, glossary_id=gid, filename="m.csv"
            ),
            None,
        )
        assert res["entries"] == n, res
        with Session(engine) as s:
            nc = len(
                s.exec(select(GlossaryConcept).where(GlossaryConcept.glossary_id == gid)).all()
            )
        assert nc == n, f"expected {n} concepts, got {nc}"
        # BOUNDED: the batched+expunged reconcile keeps peak O(batch). An O(N) reconcile
        # (4x the rows held live) would land near ~33x; the 25x bound distinguishes them.
        assert peak < 25 * size, (
            f"module import of a {size / 1e6:.1f} MB / {n}-term CSV peaked at "
            f"{peak / 1e6:.1f} MB ({peak / size:.1f}x the file) -- the reconcile is not bounded"
        )
        # RE-IMPORT: idempotent (no duplicate concepts) and still bounded.
        peak2, _s2, _res2 = _peak_import(
            path,
            lambda s, data: glossary.import_bytes(
                s, None, data, SRC, TGT, glossary_id=gid, filename="m.csv"
            ),
            None,
        )
        with Session(engine) as s:
            nc2 = len(
                s.exec(select(GlossaryConcept).where(GlossaryConcept.glossary_id == gid)).all()
            )
        assert nc2 == n, f"re-import duplicated concepts: {nc2} != {n}"
        assert peak2 < 25 * size, "re-import reconcile is not bounded"
        print(
            f"  bounded memory (MODULE dual-write): {size / 1e6:.1f} MB / {n} terms -> "
            f"{nc} concepts, {peak / 1e6:.1f} MB peak ({peak / size:.1f}x); re-import "
            f"idempotent ({nc2} concepts, {peak2 / size:.1f}x) OK"
        )
    finally:
        os.remove(path)


def test_dense_tbx_module_glossary_peak_heap_is_bounded():
    """MODULE-glossary TBX arm (round-1 H1). The DEFAULT module TBX import must ride the
    linear/bounded entry path (parse_rows -> _apply_rows -> the batched reconcile), exactly
    like the module CSV arm above -- NOT the concept-preserving path, which materialises the
    whole document as a v1 dict and re-projects (the gate measured a dense TBX peaking at
    ~58x the file that way, well over this bound).

    Counterfactual: on the pre-fix branch a module TBX import took the concept path
    unconditionally (glossary.import_bytes routed `glossary_id is not None` straight to the
    concept-preserving importer), so this 25x bound FAILS; the entry path is the default -- and,
    as of the T2 scope decision (fix round 7), the ONLY -- TBX import path, so it passes with wide
    margin. The concept-preserving TBX IMPORT is deferred to T1b; `concepts=True` is refused."""
    from app.models import GlossaryConcept

    n = 3000
    path = _write_temp("dense_module.tbx", _tbx_doc(n))
    try:
        gid = _module_glossary()
        peak, size, res = _peak_import(
            path,
            lambda s, data: glossary.import_bytes(
                s, None, data, SRC, TGT, glossary_id=gid, filename="m.tbx"
            ),
            None,
        )
        assert res["format"] == "tbx" and res["entries"] == n, res
        with Session(engine) as s:
            nc = len(
                s.exec(select(GlossaryConcept).where(GlossaryConcept.glossary_id == gid)).all()
            )
        assert nc == n, f"expected {n} concepts, got {nc}"
        assert peak < 25 * size, (
            f"module TBX import of a {size / 1e6:.1f} MB / {n}-term file peaked at "
            f"{peak / 1e6:.1f} MB ({peak / size:.1f}x the file) -- the DEFAULT TBX import is "
            "not on the bounded entry path (H1 regression?)"
        )
        print(
            f"  bounded memory (MODULE TBX default): {size / 1e6:.1f} MB / {n} terms -> "
            f"{nc} concepts, {peak / 1e6:.1f} MB peak ({peak / size:.1f}x, bound 25x) OK"
        )
    finally:
        os.remove(path)


def test_mutation_probe_batching_is_load_bearing():
    """MUTATION PROBE (the "prove it fails pre-change" requirement): the SAME
    dense TBX fixture, imported twice -- once with the shipped batch size, once
    with GLOSSARY_IMPORT_FLUSH_BATCH defeated (set far larger than the file), so
    every brand-new term's ORM object stays LIVE until the single trailing
    flush -- the closest in-repo reproduction of the pre-batching shape (one
    live GlossaryEntry per distinct term for the whole import, held instead of
    expunged), short of resurrecting the old module from git history.

    Compares the two peaks DIRECTLY (not each against file size, which the CSV
    test above shows is format-dependent and not itself the invariant that
    matters): defeating the periodic flush must cost several times the peak
    heap of the identical bytes on the batched path, or the batching is not
    actually doing anything and the bounded-memory tests above would be passing
    by accident rather than by the fix."""
    n = 12000
    path = _write_temp("mutation.tbx", _tbx_doc(n))
    original = glossary.GLOSSARY_IMPORT_FLUSH_BATCH
    original_chunk = glossary.GLOSSARY_WRITE_CHUNK_ROWS
    try:
        pid_batched = _project("Mutation TBX batched")
        peak_batched, size, res_b = _peak_import(
            path,
            lambda s, data: glossary.import_bytes(s, pid_batched, data, SRC, TGT, filename="m.tbx"),
            pid_batched,
        )
        assert res_b["entries"] == n, res_b

        set_pieces(
            "app.services.glossary", "GLOSSARY_IMPORT_FLUSH_BATCH", 10**9
        )  # never flushes early
        # #615: the write also goes out in chunks of GLOSSARY_WRITE_CHUNK_ROWS, which bound the
        # per-statement footprint on their own; defeat that bound too (one chunk), or the probe
        # would measure the chunking and not the batching.
        set_pieces("app.services.glossary", "GLOSSARY_WRITE_CHUNK_ROWS", 10**9)
        pid_defeated = _project("Mutation TBX defeated")
        peak_defeated, _size2, res_d = _peak_import(
            path,
            lambda s, data: glossary.import_bytes(
                s, pid_defeated, data, SRC, TGT, filename="m.tbx"
            ),
            pid_defeated,
        )
        assert res_d["entries"] == n, res_d

        ratio = peak_defeated / peak_batched
        assert ratio > 2, (
            f"defeating the periodic flush only changed peak heap {ratio:.2f}x "
            "on identical bytes -- batching should matter far more than that, "
            "or it is not the batching that bounds memory"
        )
        assert peak_defeated > 5 * size, (
            "the defeated run should also blow the TBX test's own 5x-file-size "
            f"bound (measured {peak_defeated / size:.1f}x) -- if it does not, the "
            "batched test above may be passing regardless of the fix"
        )
        print(
            f"  mutation probe: same {size / 1e6:.1f} MB / {n}-term TBX peaked at "
            f"{peak_batched / 1e6:.1f} MB batched vs {peak_defeated / 1e6:.1f} MB "
            f"({ratio:.1f}x) with the periodic flush defeated -- confirms the "
            "batching, not luck, is what bounds memory OK"
        )
    finally:
        set_pieces("app.services.glossary", "GLOSSARY_IMPORT_FLUSH_BATCH", original)
        set_pieces("app.services.glossary", "GLOSSARY_WRITE_CHUNK_ROWS", original_chunk)
        os.remove(path)


def test_atomicity_failure_after_flushed_batches_persists_nothing():
    """A TBX that goes malformed only AFTER several FULL batches have already
    been flushed to the DB. Only ROLLBACK TO SAVEPOINT can undo rows that are
    already written; a per-object undo (the pre-batching shape) could not."""
    n = glossary.GLOSSARY_IMPORT_FLUSH_BATCH * 3 + 7
    good = (
        f'<conceptEntry id="c{i}">'
        f'<langSec xml:lang="{SRC}"><termSec><term>Good {i}</term></termSec></langSec>'
        f'<langSec xml:lang="{TGT}"><termSec><term>Bueno {i}</term></termSec></langSec>'
        f"</conceptEntry>"
        for i in range(n)
    )
    truncated = (
        '<?xml version="1.0" encoding="utf-8"?>\n<tbx xml:lang="en">\n<text><body>'
        + "".join(good)
        + f'<conceptEntry id="broken"><langSec xml:lang="{SRC}"><termSec><term>tail'
    ).encode("utf-8")

    pid = _project("Flushed then broken")
    # seed one row so the UPDATE branch would be exercised too, had it survived
    with Session(engine) as s:
        seeded = glossary.import_csv(
            s, pid, b"source_term,target_term,dnt\r\nSeed,Semilla,\r\n", SRC, TGT
        )
    assert seeded == {"entries": 1, "senses": 0}, seeded

    raised = False
    try:
        with Session(engine) as s:
            glossary.import_bytes(s, pid, truncated, SRC, TGT, filename="broken.tbx")
            s.commit()
    except Exception:
        raised = True
    assert raised, "a truncated TBX must still raise"

    with Session(engine) as s2:
        rows = s2.exec(select(GlossaryEntry).where(GlossaryEntry.project_id == pid)).all()
        assert [(r.source_term, r.target_term) for r in rows] == [("Seed", "Semilla")], (
            "a failed import after several flushed batches must persist NOTHING "
            "beyond what existed before it started",
            [r.source_term for r in rows],
        )

    # the session (and the DB) are still usable afterwards.
    with Session(engine) as s3:
        ok = glossary.import_csv(
            s3, pid, b"source_term,target_term,dnt\r\nAfter,Despues,\r\n", SRC, TGT
        )
        assert ok == {"entries": 1, "senses": 0}, ok
    print(
        f"  atomicity: a TBX malformed after {n // glossary.GLOSSARY_IMPORT_FLUSH_BATCH} "
        f"flushed batches ({n} good entries) persists NOTHING beyond the pre-existing "
        f"row (checked from an independent session), and the project stays usable OK"
    )


def _straddle_csv(batch, second_target):
    """A duplicate source_term whose two occurrences are separated by more than
    one batch, so the first is flushed AND expunged before the second is read."""
    lines = ["source_term,target_term,dnt,sense_label,context_cue", "DUPLICATE,first target,,,"]
    lines.extend(f"Filler {i},Bourre {i},,," for i in range(batch * 2 + 1))
    lines.append(f"DUPLICATE,{second_target},,,")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8"), len(lines) - 1


def test_in_file_duplicate_straddling_a_flush_boundary():
    """The dedupe cache used to hold the live ORM object for a just-inserted
    entry, so a repeated source_term mutated that SAME live object regardless of
    batch size. Now the entry is flushed + expunged at the batch boundary, so the
    second occurrence must resolve against its id -- an UPDATE of the same row,
    never a second INSERT. Run at several batch sizes (incl. 1 and the shipped
    default) for both a CHANGED and an IDENTICAL second target."""
    original = glossary.GLOSSARY_IMPORT_FLUSH_BATCH
    try:
        for batch in (1, 2, 3, 7, original):
            set_pieces("app.services.glossary", "GLOSSARY_IMPORT_FLUSH_BATCH", batch)
            for label, second, exp_target in (
                ("changed", "second target", "second target"),
                ("identical", "first target", "first target"),
            ):
                data, n_rows = _straddle_csv(batch, second)
                pid = _project(f"Straddle {batch} {label}")
                with Session(engine) as s:
                    res = glossary.import_csv(s, pid, data, SRC, TGT)
                assert res == {"entries": n_rows - 1, "senses": 0}, (batch, label, res)
                with Session(engine) as s:
                    dup = s.exec(
                        select(GlossaryEntry).where(
                            GlossaryEntry.project_id == pid,
                            GlossaryEntry.source_term == "DUPLICATE",
                        )
                    ).all()
                assert len(dup) == 1, (batch, label, len(dup))
                assert dup[0].target_term == exp_target, (batch, label, dup[0].target_term)
    finally:
        set_pieces("app.services.glossary", "GLOSSARY_IMPORT_FLUSH_BATCH", original)
    print(
        "  straddling duplicate: an in-file repeated source_term separated by "
        "more than one flush boundary updates the SAME row (one row, last "
        "target wins) at batch sizes 1/2/3/7/default, changed and identical OK"
    )


def test_in_file_tbx_duplicate_straddling_a_flush_boundary():
    """Same claim, TBX format: a repeated source_term (two conceptEntry ids,
    same source term text) straddling a flush boundary resolves as one row."""
    original = glossary.GLOSSARY_IMPORT_FLUSH_BATCH
    try:
        batch = 3
        set_pieces("app.services.glossary", "GLOSSARY_IMPORT_FLUSH_BATCH", batch)
        parts = [
            f'<conceptEntry id="c0"><langSec xml:lang="{SRC}"><termSec>'
            f"<term>DUP</term></termSec></langSec>"
            f'<langSec xml:lang="{TGT}"><termSec><term>first</term>'
            f"</termSec></langSec></conceptEntry>"
        ]
        parts.extend(
            f'<conceptEntry id="f{i}"><langSec xml:lang="{SRC}"><termSec>'
            f"<term>Filler {i}</term></termSec></langSec>"
            f'<langSec xml:lang="{TGT}"><termSec><term>Bourre {i}</term>'
            f"</termSec></langSec></conceptEntry>"
            for i in range(batch * 2 + 1)
        )
        parts.append(
            f'<conceptEntry id="c1"><langSec xml:lang="{SRC}"><termSec>'
            f"<term>DUP</term></termSec></langSec>"
            f'<langSec xml:lang="{TGT}"><termSec><term>second</term>'
            f"</termSec></langSec></conceptEntry>"
        )
        data = (
            '<?xml version="1.0"?><tbx xml:lang="en"><text><body>'
            + "".join(parts)
            + "</body></text></tbx>"
        ).encode("utf-8")
        pid = _project("TBX straddle")
        with Session(engine) as s:
            res = glossary.import_bytes(s, pid, data, SRC, TGT, filename="d.tbx")
        assert res["skipped"] == 0, res
        with Session(engine) as s:
            dup = s.exec(
                select(GlossaryEntry).where(
                    GlossaryEntry.project_id == pid, GlossaryEntry.source_term == "DUP"
                )
            ).all()
        assert len(dup) == 1 and dup[0].target_term == "second", (
            "two SEPARATE conceptEntry elements sharing a source term are two "
            "separate BASE rows through _apply_rows, so the same last-wins "
            "semantics as CSV apply (the 'first target wins' rule in "
            "glossary_tbx._rows_for_entry is a DIFFERENT case: multiple target "
            "TERMS listed inside ONE entry's target langSec) -- one row, "
            "never two",
            [d.target_term for d in dup],
        )
    finally:
        set_pieces("app.services.glossary", "GLOSSARY_IMPORT_FLUSH_BATCH", original)
    print(
        "  straddling duplicate (TBX): a repeated source term across a flush "
        "boundary still resolves to ONE row, never a duplicate insert OK"
    )


_MIXED_CSV = (
    b"source_term,target_term,dnt,sense_label,context_cue,part_of_speech\r\n"
    b"widget,artilugio,,,,\r\n"
    b"Acme,Acme,true,,,\r\n"
    b"bank,banco,,finance,money savings account,\r\n"
    b"bank,orilla,,river,river water shore,\r\n"
    b"widget,gadget,,,,\r\n"  # in-file dup, changed target
    b"repeat,uno,,,,\r\n"
    b"repeat,uno,,,,\r\n"  # in-file dup, identical target
    b",orphan,,,,\r\n"  # blank source_term -> skipped
)


def test_counts_and_rows_invariant_under_batch_size():
    """Batching must be INVISIBLE: the same file produces the same counts and
    the same stored rows whether flushed once, per row, or anywhere between."""
    original = glossary.GLOSSARY_IMPORT_FLUSH_BATCH
    baseline = None
    try:
        for batch in (1, 2, 3, 5, original, 100000):
            set_pieces("app.services.glossary", "GLOSSARY_IMPORT_FLUSH_BATCH", batch)
            pid = _project(f"Invariant {batch}")
            with Session(engine) as s:
                res = glossary.import_csv(s, pid, _MIXED_CSV, SRC, TGT)
            terms = _terms(pid)
            got = (res, terms)
            if baseline is None:
                baseline = got
                # 4 distinct new entries (widget, Acme, bank, repeat); 2 senses.
                assert res == {"entries": 4, "senses": 2}, res
                assert terms["widget"] == ("gadget", False, {}), terms["widget"]
                assert terms["Acme"] == ("Acme", True, {}), terms["Acme"]
                assert terms["repeat"] == ("uno", False, {}), terms["repeat"]
                assert "" not in terms, "a blank source_term must never be stored"
            else:
                assert got == baseline, (batch, got, baseline)
    finally:
        set_pieces("app.services.glossary", "GLOSSARY_IMPORT_FLUSH_BATCH", original)
    print(
        "  batch invariance: counts, senses and stored term state are identical "
        "at batch sizes 1/2/3/5/default/whole-file for a mixed fixture (dnt, "
        "senses, an in-file duplicate, a blank source_term) OK"
    )


def test_reimport_updates_preloaded_existing_entries():
    """The path unexercised by every pre-existing glossary/TBX suite: a SECOND
    import into an ALREADY-POPULATED project must update the PRELOADED (not
    freshly-created) entries -- target_term, dnt and meta -- via the new
    primitives-only + bulk-UPDATE path, and a query on the SAME session that
    just wrote them must see the new values (not a stale cached read)."""
    pid = _project("Reimport")
    first = (
        b"source_term,target_term,dnt,sense_label,context_cue,part_of_speech\r\n"
        b"widget,artilugio,,,,noun\r\n"
        b"Acme,,true,,,\r\n"
    )
    with Session(engine) as s:
        res1 = glossary.import_csv(s, pid, first, SRC, TGT)
    assert res1 == {"entries": 2, "senses": 0}, res1

    second = (
        b"source_term,target_term,dnt,sense_label,context_cue,definition\r\n"
        b"widget,artefacto,,,,A small UI control.\r\n"  # target + new meta
        b"Acme,Acme,true,,,\r\n"  # gains a target
        b"NewTerm,Nuevo,,,,\r\n"
    )  # brand new
    with Session(engine) as s:
        res2 = glossary.import_csv(s, pid, second, SRC, TGT)
        assert res2 == {"entries": 1, "senses": 0}, res2
        # SAME-SESSION read, right after the bulk UPDATE that touched these rows.
        widget = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.project_id == pid, GlossaryEntry.source_term == "widget"
            )
        ).first()
        assert widget.target_term == "artefacto", widget.target_term
        assert widget.meta == {"part_of_speech": "noun", "definition": "A small UI control."}, (
            widget.meta
        )
        acme = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.project_id == pid, GlossaryEntry.source_term == "Acme"
            )
        ).first()
        assert acme.target_term == "Acme" and acme.dnt is True, (acme.target_term, acme.dnt)

    # a FRESH session (a later request) sees the identical, durable state.
    terms = _terms(pid)
    assert terms["widget"] == (
        "artefacto",
        False,
        {"part_of_speech": "noun", "definition": "A small UI control."},
    ), terms["widget"]
    assert terms["Acme"] == ("Acme", True, {}), terms["Acme"]
    assert terms["NewTerm"] == ("Nuevo", False, {}), terms["NewTerm"]
    print(
        "  re-import: a second import into an already-populated project updates "
        "PRELOADED entries (target_term/dnt/meta) via the bulk-UPDATE path, "
        "visible both in-session and from a fresh session OK"
    )


def test_import_counters_reconcile_with_rows_seen():
    """BUG-09 (QA campaign finding, 2026-07-29): reported import counts did not
    add up against the rows actually in the file. Root cause: `_apply_rows`
    counted a brand-new term ONCE (on its first row, in "entries") but a row
    touching a term ALREADY known to the call -- pre-existing in the DB, or
    created by an earlier row of the SAME import -- contributed to NEITHER
    counter, so a re-import of entirely-pre-existing terms reported "0 entries,
    0 senses" despite writing real updates, and glossary_tbx's parse-level
    `concepts` could exceed `entries + skipped` whenever a TBX repeated a source
    term across two conceptEntry elements. Fixed with an "updated" counter (a
    row for an already-known term) and a "skipped" counter (a blank
    source_term, previously silently dropped and uncounted).

    This pins the reconciling identity on the seam every user-facing surface
    actually calls (import_bytes, NOT the narrower legacy import_csv): ROWS ==
    entries + updated + skipped_rows, for CSV and TBX alike.

    REVIEW ROUND 1 corrected two things the first version of this test asserted:
    (a) the blank-source-row counter is "skipped_rows", NOT "skipped" -- see
    test_csv_arm_emits_no_skipped_key below for the user-visible reason; and
    (b) `concepts` does NOT reconcile term-for-term against entries+updated at
    all -- see test_synonym_concept_breaks_the_concepts_identity."""
    pid = _project("Counters CSV")
    # 4 data rows: 2 brand-new terms, 1 re-touch of a same-call term (bank's
    # second sense), 1 blank source_term.
    csv1 = (
        b"source_term,target_term,dnt,sense_label,context_cue\r\n"
        b"widget,artilugio,,,\r\n"
        b"bank,banco,,finance,money savings account\r\n"
        b"bank,orilla,,river,river water shore\r\n"
        b",orphan,,,\r\n"
    )
    with Session(engine) as s:
        res1 = glossary.import_bytes(s, pid, csv1, SRC, TGT)
    assert res1["entries"] == 2 and res1["senses"] == 2, res1
    assert res1["updated"] == 1 and res1["skipped_rows"] == 1, res1
    assert res1["entries"] + res1["updated"] + res1["skipped_rows"] == 4, (
        "entries+updated+skipped_rows must equal the 4 non-blank+blank data rows",
        res1,
    )

    # a SECOND import touching only PRE-EXISTING terms must report them as
    # "updated", never silently as "0 entries, 0 senses" (the reported bug).
    csv2 = b"source_term,target_term,dnt\r\nwidget,artefacto,\r\nbank,banco,\r\n"
    with Session(engine) as s:
        res2 = glossary.import_bytes(s, pid, csv2, SRC, TGT)
    assert res2["entries"] == 0, res2
    assert res2["updated"] == 2, ("both rows touch PRE-EXISTING terms", res2)
    assert res2["skipped_rows"] == 0, res2
    assert res2["entries"] + res2["updated"] + res2["skipped_rows"] == 2, res2

    # TBX: two conceptEntry elements sharing one source term ("DUP") plus one
    # unusable concept (no target side) -- concepts must reconcile too.
    pid2 = _project("Counters TBX")
    tbx = (
        '<?xml version="1.0"?><tbx xml:lang="en"><text><body>'
        f'<conceptEntry id="c0"><langSec xml:lang="{SRC}"><termSec><term>DUP</term>'
        f'</termSec></langSec><langSec xml:lang="{TGT}"><termSec><term>uno</term>'
        f"</termSec></langSec></conceptEntry>"
        f'<conceptEntry id="c1"><langSec xml:lang="{SRC}"><termSec><term>DUP</term>'
        f'</termSec></langSec><langSec xml:lang="{TGT}"><termSec><term>dos</term>'
        f"</termSec></langSec></conceptEntry>"
        f'<conceptEntry id="c2"><langSec xml:lang="{SRC}"><termSec><term>orphan</term>'
        f"</termSec></langSec></conceptEntry>"
        "</body></text></tbx>"
    ).encode()
    with Session(engine) as s:
        res3 = glossary.import_bytes(s, pid2, tbx, SRC, TGT, filename="d.tbx")
    assert res3["concepts"] == 3, res3
    assert res3["entries"] == 1 and res3["updated"] == 1 and res3["skipped"] == 1, res3
    assert res3["skipped_rows"] == 0, (
        "glossary_tbx never emits a blank source_term, so the row-level counter "
        "is 0 -- and it is NOT folded into the TBX 'skipped' key",
        res3,
    )
    # This fixture happens to be one-source-term-per-concept, so the counts line
    # up exactly; that is a property of THIS file, not an invariant -- see
    # test_synonym_concept_breaks_the_concepts_identity.
    assert res3["entries"] + res3["updated"] >= res3["concepts"] - res3["skipped"], res3
    with Session(engine) as s:
        dup = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.project_id == pid2, GlossaryEntry.source_term == "DUP"
            )
        ).all()
    assert len(dup) == 1, dup

    # import_csv's own public return is UNTOUCHED (the explicit legacy contract).
    with Session(engine) as s:
        legacy = glossary.import_csv(
            s, _project("Counters legacy"), b"source_term,target_term\r\nX,Y\r\n", SRC, TGT
        )
    assert legacy == {"entries": 1, "senses": 0}, (
        "import_csv's direct return must stay the narrow legacy shape",
        legacy,
    )
    print(
        "  import counters reconcile (BUG-09): entries+updated+skipped_rows "
        "accounts for every CSV row (incl. blank source_term) and every TBX "
        "row; import_csv's own return is unchanged OK"
    )


def test_csv_arm_emits_no_skipped_key():
    """REVIEW ROUND 1 REGRESSION. "skipped" is a TBX PARSE-LEVEL fact and every
    surface renders it that way -- the project page and the glossary page print
    "Skipped N TBX entr(ies) with no matching language pair", and both CLI import
    verbs print "Skipped N entries with no matching language pair". The first
    version of the counter fix folded _apply_rows's BLANK-SOURCE-ROW count into
    that same key and emitted it for CSV imports too, so an ordinary
    Excel-exported CSV -- which ends in empty rows constantly -- told the user
    that N entries had no matching language pair. Reproduced on the shipped
    templates before the fix.

    The counter itself is kept (it is the point of BUG-09), under its own name."""
    pid = _project("Excel trailing rows")
    data = (
        b"source_term,target_term,dnt,sense_label,context_cue\r\n"
        b"widget,artilugio,,,\r\n"
        b",,,,\r\n"
        b",,,,\r\n"
    )
    with Session(engine) as s:
        res = glossary.import_bytes(s, pid, data, SRC, TGT, filename="x.csv")
    assert res["format"] == "csv" and res["entries"] == 1, res
    assert res["skipped_rows"] == 2, ("the blank rows are still counted", res)
    assert "skipped" not in res, (
        "a CSV import must emit NO 'skipped' key -- the web templates and the CLI "
        "render it as 'entries with no matching language pair', which is a TBX "
        "parse-level fact and a lie about a CSV's blank rows",
        res,
    )
    print(
        "  CSV arm emits no 'skipped' key: blank rows are counted as "
        "'skipped_rows' instead, so no surface reports them as 'entries with "
        "no matching language pair' OK"
    )


# --- changed/unchanged counters (the approval-revert fix, 2026-07-29) --------
# 8 plain rows + 1 sense row. The mutated row is t6 -- deep enough that at the
# small batch sizes below its entry was created, FLUSHED and EXPUNGED in an
# EARLIER batch of both the seed import and the re-import, so change detection
# is exercised on the primitives-only/bulk-UPDATE path, not just on a live ORM
# object still sitting in the current batch.
_CHANGE_SEED = (
    b"source_term,target_term,dnt,sense_label,context_cue,definition\r\n"
    + b"".join(f"t{i},v{i},,,,\r\n".encode("ascii") for i in range(8))
    + b"bank,banco,,finance,money savings,\r\n"
)


def _senses(pid):
    with Session(engine) as s:
        ids = [
            e.id for e in s.exec(select(GlossaryEntry).where(GlossaryEntry.project_id == pid)).all()
        ]
        rows = s.exec(select(GlossarySense).where(GlossarySense.glossary_entry_id.in_(ids))).all()
        return sorted((r.sense_label, r.context_cue, r.target_term) for r in rows)


def test_changed_counter_is_batch_invariant():
    """The value-changed counters must be a property of the FILE, not of where
    the flush boundaries happen to fall.

    `changed` is what glossary_module.import_bytes reverts an APPROVED glossary
    on, so a batch-size-dependent answer would mean a published glossary's draft
    state depended on GLOSSARY_IMPORT_FLUSH_BATCH -- and the risky direction is
    the cheap one to get wrong: a term whose entry was expunged at a batch
    boundary is compared through the primitives cache, so a naive
    "is the ORM object dirty?" test would report every such row as changed."""
    original = glossary.GLOSSARY_IMPORT_FLUSH_BATCH
    cases = (
        ("identical", _CHANGE_SEED, 0),
        ("changed target", _CHANGE_SEED.replace(b"t6,v6,", b"t6,SIX,"), 1),
        ("dnt flip", _CHANGE_SEED.replace(b"t6,v6,,,,", b"t6,v6,true,,,"), 1),
        ("meta only", _CHANGE_SEED.replace(b"t6,v6,,,,\r\n", b"t6,v6,,,,a widget\r\n"), 1),
        (
            "edited sense",
            _CHANGE_SEED.replace(
                b"bank,banco,,finance,money savings,", b"bank,banca,,finance,money savings,"
            ),
            1,
        ),
    )
    baselines = {}
    try:
        for batch in (1, 2, 3, 5, original, 100000):
            set_pieces("app.services.glossary", "GLOSSARY_IMPORT_FLUSH_BATCH", batch)
            for name, data, expect in cases:
                pid = _project(f"Changed {batch} {name}")
                with Session(engine) as s:
                    seed = glossary.import_bytes(s, pid, _CHANGE_SEED, SRC, TGT)
                assert seed["entries"] == 9 and seed["senses"] == 1, (batch, seed)
                with Session(engine) as s:
                    res = glossary.import_bytes(s, pid, data, SRC, TGT)
                assert res["entries"] == 0, (batch, name, res)
                assert res["senses"] == 0, (
                    "a re-imported sense row UPSERTS -- it never counts as a new "
                    "sense, and never inserts a second row",
                    batch,
                    name,
                    res,
                )
                assert res["changed"] == expect, (batch, name, res)
                assert res["unchanged"] == 9 - expect, (batch, name, res)
                assert res["changed"] + res["unchanged"] == res["updated"], (batch, name, res)
                assert res["entries"] + res["updated"] + res["skipped_rows"] == 9, (
                    batch,
                    name,
                    res,
                )
                assert len(_senses(pid)) == 1, ("one sense row, not two", batch, name, _senses(pid))
                got = (res, _terms(pid), _senses(pid))
                if name in baselines:
                    assert got == baselines[name], (batch, name, got, baselines[name])
                else:
                    baselines[name] = got
    finally:
        set_pieces("app.services.glossary", "GLOSSARY_IMPORT_FLUSH_BATCH", original)
    assert baselines["edited sense"][2] == [("finance", "money savings", "banca")], baselines[
        "edited sense"
    ][2]
    print(
        "  changed/unchanged counters are batch-invariant: an identical "
        "re-import reports 0 changed / 9 unchanged and a single changed target, "
        "DNT flip, meta cell or sense target reports exactly 1 changed -- at "
        "batch sizes 1/2/3/5/default/whole-file, with the mutated row straddling "
        "a flush boundary OK"
    )


def test_in_file_duplicate_sense_straddling_a_flush_boundary():
    """A repeated (source_term, sense_label) pair inside ONE file resolves to ONE
    GlossarySense row carrying the LAST value -- the sense-level twin of the
    straddling-duplicate rule for entries, and previously a straight second
    INSERT (which is what made every sense-bearing re-import look like it had
    created content, masking the approval-revert bug)."""
    original = glossary.GLOSSARY_IMPORT_FLUSH_BATCH
    try:
        for batch in (1, 2, 3, original):
            set_pieces("app.services.glossary", "GLOSSARY_IMPORT_FLUSH_BATCH", batch)
            data = (
                b"source_term,target_term,dnt,sense_label,context_cue\r\n"
                b"bank,banco,,finance,money\r\n"
                b"filler1,uno,,,\r\n"
                b"filler2,dos,,,\r\n"
                b"filler3,tres,,,\r\n"
                b"bank,banca,,finance,money markets\r\n"
            )
            pid = _project(f"Dup sense {batch}")
            with Session(engine) as s:
                res = glossary.import_bytes(s, pid, data, SRC, TGT)
            assert res["senses"] == 1, ("one sense CREATED, not two", batch, res)
            assert res["changed"] == 1 and res["updated"] == 1, (batch, res)
            assert _senses(pid) == [("finance", "money markets", "banca")], (batch, _senses(pid))
    finally:
        set_pieces("app.services.glossary", "GLOSSARY_IMPORT_FLUSH_BATCH", original)
    print(
        "  in-file duplicate SENSE across a flush boundary: one row, last value "
        "wins (never a second INSERT), at batch sizes 1/2/3/default OK"
    )


def test_synonym_concept_breaks_the_concepts_identity():
    """REVIEW ROUND 1 REGRESSION. The counter fix's docstrings (and this suite)
    asserted `concepts == parse-skipped + entries + updated`. That is FALSE for
    the single most ordinary shape a real termbase has: ONE conceptEntry listing
    several source terms (synonyms) in its source langSet. glossary_tbx emits one
    ROW PER DISTINCT SOURCE TERM, so a 3-synonym concept yields entries=3 with
    concepts=1 -- reproduced. Both Microsoft Terminology and IATE ship files like
    this, so the "identity" would have failed on the first real termbase.

    The only true relation is the INEQUALITY pinned below. This test exists so
    the equality cannot be re-asserted anywhere."""
    tbx = (
        '<?xml version="1.0"?><tbx xml:lang="en"><text><body>'
        f'<conceptEntry id="c0"><langSec xml:lang="{SRC}">'
        "<termSec><term>car</term></termSec>"
        "<termSec><term>automobile</term></termSec>"
        "<termSec><term>motorcar</term></termSec>"
        f'</langSec><langSec xml:lang="{TGT}"><termSec><term>coche</term>'
        "</termSec></langSec></conceptEntry>"
        f'<conceptEntry id="c1"><langSec xml:lang="{SRC}"><termSec>'
        "<term>orphan</term></termSec></langSec></conceptEntry>"
        "</body></text></tbx>"
    ).encode()
    pid = _project("Synonym concept")
    with Session(engine) as s:
        res = glossary.import_bytes(s, pid, tbx, SRC, TGT, filename="syn.tbx")
    assert res["concepts"] == 2 and res["skipped"] == 1, res
    assert res["entries"] == 3 and res["updated"] == 0, (
        "one concept with three source terms is THREE rows / three entries",
        res,
    )
    assert res["concepts"] != res["skipped"] + res["entries"] + res["updated"], (
        "this fixture must BREAK the old, false identity -- if it ever holds "
        "here, _rows_for_entry stopped emitting one row per source term",
        res,
    )
    assert res["entries"] + res["updated"] >= res["concepts"] - res["skipped"], (
        "the TRUE relation: every usable concept contributes at least one row",
        res,
    )
    assert set(_terms(pid)) == {"car", "automobile", "motorcar"}, _terms(pid)
    assert all(_terms(pid)[t][0] == "coche" for t in _terms(pid)), _terms(pid)
    print(
        "  synonym concept: one conceptEntry with three source terms imports as "
        "three entries, so `concepts` reconciles only as an INEQUALITY against "
        "entries+updated (the old equality is pinned as false) OK"
    )


def main():
    init_db()
    test_dense_tbx_peak_heap_is_bounded()
    test_dense_csv_peak_heap_is_bounded()
    test_dense_csv_module_glossary_peak_heap_is_bounded()
    test_dense_tbx_module_glossary_peak_heap_is_bounded()
    test_mutation_probe_batching_is_load_bearing()
    test_atomicity_failure_after_flushed_batches_persists_nothing()
    test_in_file_duplicate_straddling_a_flush_boundary()
    test_in_file_tbx_duplicate_straddling_a_flush_boundary()
    test_counts_and_rows_invariant_under_batch_size()
    test_reimport_updates_preloaded_existing_entries()
    test_import_counters_reconcile_with_rows_seen()
    test_csv_arm_emits_no_skipped_key()
    test_changed_counter_is_batch_invariant()
    test_in_file_duplicate_sense_straddling_a_flush_boundary()
    test_synonym_concept_breaks_the_concepts_identity()
    print("GLOSSARY IMPORT BOUNDED MEMORY OK")


if __name__ == "__main__":
    main()
