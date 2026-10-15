"""glossary bulk-import SCALING + EQUIVALENCE verification (terminology-bulk train) -- hermetic.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys, zero network.

Context: T1-lite (merge 1e91de9f) added a per-entry concept dual-write reconcile inside the
glossary bulk importer (glossary._apply_rows -> glossary_concepts.reconcile_glossary_from_
entries). That per-entry path (sync_entry_concept) was O(n^2) -- _find_concept_for_source
scanned the whole glossary on every first-seen source surface (no source_term index) -- AND
paid one flush per concept/term, so importing the real 61,956-termEntry Microsoft Terminology
TBX crawled (~250 entries/min, killed after 20 min). This train replaces the importer's
reconcile with glossary_concepts._bulk_sync_glossary_from_entries: the concept/term identity
maps are pre-loaded once, and each flush batch's new concepts/terms are written with a Core
RETURNING insert (DB-assigned ids, no per-row ORM construction/flush; batched on PostgreSQL,
per-row on SQLite) -- LINEAR, byte-identical store.
See the _build-errata-terminology-bulk notes.

Covers:
  equivalence-live  -- a rich fixture (senses/homonyms, DNT, all five meta keys, a casefold
                       collision, duplicate source surfaces collapsing, multi-target) imported
                       through the SHIPPED bulk path vs the RETAINED per-entry path
                       (_sync_glossary_from_entries) produce the IDENTICAL id-free store, for
                       a single import AND an incremental re-import that moves a target.
  equivalence-golden-- the shipped bulk path reproduces a COMMITTED golden (tests/
                       glossary_bulk_golden.json) captured ONCE from the per-entry (pre-change)
                       reconcile by tests/capture_glossary_bulk_golden.py -- the flow-engine
                       golden pattern. A divergence is either a real regression or an
                       intentional store change that must regenerate the golden.
  scaling-linearity -- synthetic 5k and 12.5k TBX generated in the throwaway dir: the import
                       time is LINEAR (12.5k under 3.5x + 2 s of 5k; quadratic would be ~6.25x).
                       No absolute wall-clock budget (load-dependent). Best-of-2 min per size,
                       each into a freshly-wiped store, damps timing noise.
  idempotent-reimport- re-importing the SAME file changes NOTHING (changed==0, store identical)
                       and is not slower than the first import.

Final line: "GLOSSARY BULK IMPORT SCALING OK".
"""

import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

_TMP = init_env("loctms_glossbulkscale_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import GlossaryConcept, GlossaryEntry, GlossarySense, GlossaryTerm
from app.services import glossary, glossary_concepts, glossary_module

init_db()

SRC, TGT = "en-US", "es-ES"
_GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glossary_bulk_golden.json")

# ---------------------------------------------------------------- fixtures ---
# A rich CSV that exercises every concept/term shape the bulk builder must reproduce:
# duplicate source surface (invoice -> two targets on ONE concept), a casefold collision
# (Bank/bank share a concept, distinct verbatim source terms), a DNT source, senses
# (homonym siblings), and all five meta keys on the base row.
RICH_CSV = (
    b"source_term,target_term,dnt,sense_label,context_cue,definition,subject_field,"
    b"part_of_speech,usage_example,notes\r\n"
    b"invoice,factura,,,,A bill for goods,finance,noun,Send the invoice,keep 30d\r\n"
    b"invoice,recibo,,,,,,,,\r\n"
    b"Bank,Banco,,,,,finance,,,\r\n"
    b"bank,orilla,,,,,geography,,,\r\n"
    b"sort,ordenar,,,,,,,,\r\n"
    b"sort,clasificar,finance,sort in finance,,,,,\r\n"
    b"sort,ordenar filas,tech,sort rows,,,,,\r\n"
    b"Acme,Acme,1,,,,,,,\r\n"
)

# The incremental follow-up: move a base target, keep one, add a brand-new term.
RICH_CSV_REIMPORT = (
    b"source_term,target_term,dnt,sense_label,context_cue\r\n"
    b"invoice,factura-final,,,\r\n"
    b"sort,ordenar,,,\r\n"
    b"widget,artilugio,,,\r\n"
)


def _tbx_unique(n: int) -> bytes:
    """Like _tbx_doc but every source surface is DISTINCT (no collapse), so a re-import is a
    true no-op (changed==0) -- the clean idempotency shape. (A file with in-file duplicate
    surfaces that disagree makes _apply_rows conservatively over-report `changed` on every
    re-import: it writes A then B for the shared term and counts BOTH, by design.)"""
    parts = [
        '<?xml version="1.0" encoding="utf-8"?><martif type="TBX" xml:lang="en-US">'
        "<martifHeader><fileDesc><titleStmt><title>u</title></titleStmt>"
        "<sourceDesc><p>u</p></sourceDesc></fileDesc></martifHeader><text><body>"
    ]
    for i in range(n):
        parts.append(
            f'<termEntry id="u{i}">'
            f'<langSet xml:lang="en-US"><ntig><termGrp><term id="s{i}">unique surface {i}</term>'
            f"</termGrp></ntig></langSet>"
            f'<langSet xml:lang="es"><ntig><termGrp><term id="t{i}">unica {i}</term>'
            f"</termGrp></ntig></langSet></termEntry>"
        )
    parts.append("</body></text></martif>")
    return "".join(parts).encode("utf-8")


def _wipe():
    """Clear the concept/term/entry/sense tables so a timed import starts from an EMPTY store
    -- otherwise the shared suite DB grows across tests and index-depth inflation would mask
    the per-import linearity the scaling test measures. Child-before-parent (real FKs)."""
    from sqlalchemy import delete as sa_delete

    with Session(engine) as s:
        for model in (GlossarySense, GlossaryEntry, GlossaryTerm, GlossaryConcept):
            s.execute(sa_delete(model))
        s.commit()


def _tbx_doc(n: int) -> bytes:
    """A synthetic Microsoft-shaped TBX: n conceptEntries, each en-US + es langSet, a
    definition on ~every entry, and ~30% duplicate source surfaces (so the importer's
    collapse-and-update path is exercised, like the real termbase)."""
    parts = [
        '<?xml version="1.0" encoding="utf-8"?><martif type="TBX" xml:lang="en-US">'
        "<martifHeader><fileDesc><titleStmt><title>syn</title></titleStmt>"
        "<sourceDesc><p>syn</p></sourceDesc></fileDesc></martifHeader><text><body>"
    ]
    for i in range(n):
        # every 3rd entry reuses an earlier source surface (a synonym/homograph shape)
        src_i = i - 1 if (i % 3 == 0 and i > 0) else i
        parts.append(
            f'<termEntry id="c{i}">'
            f'<langSet xml:lang="en-US"><descripGrp>'
            f'<descrip type="definition">Definition number {i} of a synthetic term.</descrip>'
            f'</descripGrp><ntig><termGrp><term id="s{i}">term surface {src_i}</term>'
            f'<termNote type="partOfSpeech">Noun</termNote></termGrp></ntig></langSet>'
            f'<langSet xml:lang="es"><ntig><termGrp>'
            f'<term id="t{i}">superficie {i}</term></termGrp></ntig></langSet>'
            f"</termEntry>"
        )
    parts.append("</body></text></martif>")
    return "".join(parts).encode("utf-8")


# ------------------------------------------------------------- import driver ---
_N = 0


def _new_glossary(pairs=((SRC, TGT),)):
    global _N
    _N += 1
    with Session(engine) as s:
        g = glossary_module.create(s, name=f"BulkScale {_N}")
        glossary_module.set_pairs(s, g.id, list(pairs))
        s.commit()
        return g.id


def _import(gid, data, *, use_bulk=True, filename="f.csv"):
    """Import `data` into glossary `gid`, through the shipped bulk reconcile (use_bulk) or
    the retained per-entry reconcile (the pre-change store, for equivalence capture)."""
    orig = glossary_concepts.reconcile_glossary_from_entries
    if not use_bulk:
        set_pieces(
            "app.services.glossary_concepts",
            "reconcile_glossary_from_entries",
            # (#615: a big import reconciles per chunk and passes `entry_ids`; the retained
            # per-entry path syncs the whole glossary, which is idempotent, so it ignores them.)
            lambda session, glossary_id, **_kw: glossary_concepts._sync_glossary_from_entries(
                session, glossary_id
            ),
        )
    try:
        with Session(engine) as s:
            return glossary.import_bytes(
                s, None, data, SRC, TGT, glossary_id=gid, filename=filename
            )
    finally:
        set_pieces("app.services.glossary_concepts", "reconcile_glossary_from_entries", orig)


# ------------------------------------------------------------------ snapshot ---
def _term_sig(t):
    return [
        t.locale,
        t.term,
        t.status,
        t.term_type,
        t.part_of_speech,
        t.grammatical_gender,
        t.grammatical_number,
        t.audience,
        t.usage_note,
        t.context_example,
        t.geographical_usage,
        bool(t.case_sensitive),
        bool(t.exact),
        bool(t.dnt),
        t.source,
    ]


def _strip_hg(v):
    """Drop the glossary-id prefix ``g<id>:`` from a homonym_group so a snapshot is stable
    across glossary ids (the only id the store bakes into a comparable field)."""
    return v.split(":", 1)[1] if v and ":" in v else v


def _snapshot(gid):
    """The glossary's concept/term/entry-link/sense store as an id-free, uuid-free,
    timestamp-free, ORDER-STABLE structure -- what must be identical between the bulk path,
    the per-entry path, and the committed golden."""
    with Session(engine) as s:
        concepts = s.exec(select(GlossaryConcept).where(GlossaryConcept.glossary_id == gid)).all()
        cids = [c.id for c in concepts]
        terms = (
            s.exec(select(GlossaryTerm).where(GlossaryTerm.concept_id.in_(cids))).all()
            if cids
            else []
        )
        entries = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
        eids = [e.id for e in entries]
        senses = (
            s.exec(select(GlossarySense).where(GlossarySense.glossary_entry_id.in_(eids))).all()
            if eids
            else []
        )
        term_by_id = {t.id: t for t in terms}
        tbc = {}
        for t in terms:
            tbc.setdefault(t.concept_id, []).append(_term_sig(t))
        csig = {}
        for c in concepts:
            csig[c.id] = [
                c.domain,
                c.subdomain,
                c.definition,
                c.notes,
                _strip_hg(c.homonym_group),
                c.source,
                sorted(tbc.get(c.id, [])),
            ]
        concept_ms = sorted(csig.values())
        entry_ms = sorted(
            [
                e.source_locale,
                e.target_locale,
                e.source_term,
                e.target_term,
                bool(e.dnt),
                csig.get(e.concept_id),
                _term_sig(term_by_id[e.term_id]) if e.term_id in term_by_id else None,
            ]
            for e in entries
        )
        sense_ms = sorted(
            [
                sn.sense_label,
                sn.context_cue,
                sn.target_term,
                csig.get(sn.concept_id),
                _term_sig(term_by_id[sn.term_id]) if sn.term_id in term_by_id else None,
            ]
            for sn in senses
        )
        return {
            "n_concepts": len(concepts),
            "n_terms": len(terms),
            "n_entries": len(entries),
            "n_senses": len(senses),
            "concepts": concept_ms,
            "entries": entry_ms,
            "senses": sense_ms,
        }


def _norm(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=True)


def _assert_equal_store(a, b, label):
    for k in ("n_concepts", "n_terms", "n_entries", "n_senses"):
        assert a[k] == b[k], f"{label}: {k} {a[k]} != {b[k]}"
    for part in ("concepts", "entries", "senses"):
        assert _norm(a[part]) == _norm(b[part]), (
            f"{label}: {part} diverged "
            f"(only-A={len(set(map(_norm, a[part])) - set(map(_norm, b[part])))}, "
            f"only-B={len(set(map(_norm, b[part])) - set(map(_norm, a[part])))})"
        )


# --------------------------------------------------------------------- tests ---
def test_equivalence_live():
    """Bulk path vs the retained per-entry path: identical store on a rich single import
    AND on an incremental re-import that moves a target."""
    for label, docs in (("single", [RICH_CSV]), ("reimport", [RICH_CSV, RICH_CSV_REIMPORT])):
        gid_pe = _new_glossary()
        for d in docs:
            _import(gid_pe, d, use_bulk=False)
        gid_bk = _new_glossary()
        for d in docs:
            _import(gid_bk, d, use_bulk=True)
        _assert_equal_store(_snapshot(gid_pe), _snapshot(gid_bk), f"live[{label}]")
    # a small synthetic TBX too (no senses, the bulk-format shape)
    tbx = _tbx_doc(300)
    gid_pe = _new_glossary()
    _import(gid_pe, tbx, use_bulk=False, filename="s.tbx")
    gid_bk = _new_glossary()
    _import(gid_bk, tbx, use_bulk=True, filename="s.tbx")
    _assert_equal_store(_snapshot(gid_pe), _snapshot(gid_bk), "live[tbx300]")
    print("  equivalence-live: bulk == per-entry (rich CSV single+reimport, TBX-300) OK")


def test_equivalence_golden():
    """The shipped bulk path reproduces the committed golden captured from the per-entry
    (pre-change) reconcile."""
    assert os.path.exists(_GOLDEN), (
        f"missing golden {_GOLDEN} -- regenerate with tests/capture_glossary_bulk_golden.py"
    )
    with open(_GOLDEN, encoding="ascii") as f:
        golden = json.load(f)
    gid = _new_glossary()
    _import(gid, RICH_CSV, use_bulk=True)
    _import(gid, RICH_CSV_REIMPORT, use_bulk=True)
    got = _snapshot(gid)
    _assert_equal_store(golden, got, "golden")
    print(
        f"  equivalence-golden: bulk store byte-equal to committed golden "
        f"({got['n_concepts']} concepts / {got['n_terms']} terms) OK"
    )


def _time_import(n):
    """Best-of-2 minimum wall time (perf_counter) of importing a fresh n-term TBX through the
    shipped bulk path; also returns the entry count for a sanity assert."""
    data = _tbx_doc(n)
    best = None
    entries = None
    for _ in range(2):
        _wipe()  # each timed run imports into an EMPTY store (comparable across sizes)
        gid = _new_glossary()
        with Session(engine) as s:
            t0 = time.perf_counter()
            res = glossary.import_bytes(s, None, data, SRC, TGT, glossary_id=gid, filename="s.tbx")
            dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
        entries = res["entries"]
    return best, entries


def test_scaling_linearity_and_budget():
    """Import time is linear: 12.5k rows cost < 3.5x of 5k rows (quadratic would be ~6.25x)."""
    # Sizes are kept small (5k / 12.5k, a 2.5x row ratio) so the whole suite stays well clear of the
    # runner's 600 s cap even on a heavily loaded machine (gate round 2 M4: 8k/20k ran 372-742 s
    # under an 8-way suite run). No absolute wall-clock budget: it is load-dependent and the
    # anti-quadratic RATIO is the signal.
    t5, e5 = _time_import(5000)
    t12, e12 = _time_import(12500)
    assert e5 > 0 and e12 > 0, (e5, e12)
    # The pre-fix per-entry reconcile was O(n^2) (12.5k would cost ~6.25x of 5k); the bulk path is
    # linear-with-a-log-factor (SQLite B-tree index maintenance), so a genuinely LINEAR run sits
    # around 2.5x. The bound is 3.5x + 2 s: wide enough to absorb the log factor, per-run fixed
    # overhead and load-induced timing noise (gate round 2 measured 2.59x under load), narrow enough
    # that anything approaching the old quadratic cost fails by a wide margin.
    assert t12 < 3.5 * t5 + 2.0, (
        f"NON-LINEAR: 12.5k={t12:.2f}s vs 5k={t5:.2f}s (ratio {t12 / t5:.2f}x; "
        f"budget 3.5x + 2s = {3.5 * t5 + 2.0:.2f}s) -- O(n^2) regression?"
    )
    print(
        f"  scaling-linearity: 5k={t5:.2f}s 12.5k={t12:.2f}s (ratio {t12 / t5:.2f}x, "
        f"budget 3.5x+2s; quadratic would be ~6.25x) OK"
    )


def test_idempotent_reimport():
    """Re-importing the same file changes nothing and is not slower than the first import."""
    data = _tbx_unique(6000)  # distinct surfaces -> a true no-op re-import (changed==0)
    _wipe()
    gid = _new_glossary()
    with Session(engine) as s:
        t0 = time.perf_counter()
        glossary.import_bytes(s, None, data, SRC, TGT, glossary_id=gid, filename="s.tbx")
        dt1 = time.perf_counter() - t0
    snap1 = _snapshot(gid)
    with Session(engine) as s:
        t0 = time.perf_counter()
        second = glossary.import_bytes(s, None, data, SRC, TGT, glossary_id=gid, filename="s.tbx")
        dt2 = time.perf_counter() - t0
    snap2 = _snapshot(gid)
    assert second["changed"] == 0, f"re-import reported {second['changed']} changed rows (want 0)"
    assert second["entries"] == 0, f"re-import created {second['entries']} new entries (want 0)"
    _assert_equal_store(snap1, snap2, "idempotent")
    # not slower: the re-import touches no new rows. Allow generous CI slack (2x + 3s floor).
    assert dt2 < 2.0 * dt1 + 3.0, f"re-import {dt2:.2f}s vs first {dt1:.2f}s -- unexpectedly slower"
    print(
        f"  idempotent-reimport: 0 changed, store identical, "
        f"first={dt1:.2f}s re-import={dt2:.2f}s OK"
    )


def _sense_csv(n: int, sense_at=()):
    """n distinct base rows (source surface unique per row) with a sense (sense_label +
    context_cue) on each index in `sense_at`, base + sense carried on ONE row (the shape the
    round-1 H1 reproducer used). When n>250 the reconcile streams >1 entry batch, so a sense
    row inside an early batch (whose delegate ORM-inserts a concept) precedes a later Core
    batch -- exactly the ordering that, before the fix, made the later batch's explicit-id
    INSERT collide with the id the ORM insert had reserved."""
    lines = [b"source_term,target_term,dnt,sense_label,context_cue"]
    sset = set(sense_at)
    for i in range(n):
        if i in sset:
            lines.append(f"srcterm{i},tgt{i},,sense{i},cue{i}".encode())
        else:
            lines.append(f"srcterm{i},tgt{i},,,".encode())
    return b"\r\n".join(lines) + b"\r\n"


def test_batch_boundary_with_senses():
    """Round-1 H1: a bulk import that crosses the 250-entry reconcile batch boundary WITH a
    sense row must not raise (before the fix it raised
    ``UNIQUE constraint failed: glossaryconcept.id`` -- the sense delegate's ORM autoincrement
    insert reserved an id the next Core batch minted again). Cases straddle the boundary in the
    first, both-ends, and a far-later batch. The store must also equal the retained per-entry
    path (which never had H1), proving the boundary crossing is not just crash-free but correct."""
    cases = [(251, (0,)), (300, (0, 299)), (600, (400,))]
    for n, sense_at in cases:
        gid_bk = _new_glossary()
        res = _import(gid_bk, _sense_csv(n, sense_at), use_bulk=True)
        assert res["entries"] == n, f"n={n}: expected {n} entries, got {res['entries']}"
        assert res["senses"] == len(sense_at), (
            f"n={n}: expected {len(sense_at)} senses, got {res['senses']}"
        )
        gid_pe = _new_glossary()
        _import(gid_pe, _sense_csv(n, sense_at), use_bulk=False)
        _assert_equal_store(
            _snapshot(gid_pe), _snapshot(gid_bk), f"boundary[n={n},sense@{sorted(sense_at)}]"
        )
    print(
        "  batch-boundary-senses: 251@[0], 300@[0,299], 600@[400] import (no id collision) "
        "and match per-entry OK"
    )


def test_no_insert_carries_id():
    """H2 structural guard: no INSERT the bulk path issues into glossaryconcept/glossaryterm may
    name the ``id`` column -- supplying id explicitly never advances a Postgres SERIAL sequence,
    so a later ORM insert would collide. A Core ``before_cursor_execute`` listener inspects every
    INSERT's column list (the paramstyle-independent signal: on SQLite the executemany params are
    tuples, so a dict-key check would silently pass). Runs a boundary-crossing import with a
    sense so both the concept and term inserts AND the delegate path are exercised."""
    import re

    from sqlalchemy import event

    seen_inserts = []
    offenders = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        s = " ".join(statement.split())
        m = re.match(r"INSERT INTO (glossaryconcept|glossaryterm)\s*\(([^)]*)\)", s, re.I)
        if not m:
            return
        table = m.group(1).lower()
        cols = [c.strip().strip('"').lower() for c in m.group(2).split(",")]
        seen_inserts.append((table, cols))
        if "id" in cols:
            offenders.append((table, cols))

    event.listen(engine, "before_cursor_execute", _before)
    try:
        gid = _new_glossary()
        _import(gid, _sense_csv(300, (0, 299)), use_bulk=True)
    finally:
        event.remove(engine, "before_cursor_execute", _before)
    assert seen_inserts, "no glossaryconcept/glossaryterm INSERT was observed -- guard is vacuous"
    assert not offenders, f"bulk INSERT supplied an explicit id column (H2): {offenders}"
    print(
        f"  no-insert-carries-id: {len(seen_inserts)} concept/term INSERTs, none names the id "
        f"column (H2 guarded) OK"
    )


def test_csv_roundtrip_301_with_sense():
    """Round-1 M1: export a >250-entry module glossary that carries a sense to CSV, then re-import
    that CSV into a fresh glossary. The exporter emits the sense as its OWN row, so the re-import
    again crosses the batch boundary with a sense (the H1 shape) -- and the two stores must match.
    Before the fix even the FIRST import raised; this proves the whole round trip."""
    src = _sense_csv(301, (0,))  # 301 entries, a sense on the first (boundary at 250)
    gid_a = _new_glossary()
    res_a = _import(gid_a, src, use_bulk=True)
    assert res_a["entries"] == 301 and res_a["senses"] == 1, res_a
    with Session(engine) as s:
        exported = glossary_module.export_csv(s, gid_a)
    assert b"sense0" in exported, "exported CSV lost the sense row"
    gid_b = _new_glossary()
    res_b = _import(gid_b, exported, use_bulk=True, filename="roundtrip.csv")
    assert res_b["senses"] == 1, f"round-trip re-import lost the sense: {res_b}"
    _assert_equal_store(_snapshot(gid_a), _snapshot(gid_b), "csv-roundtrip-301")
    print(
        f"  csv-roundtrip-301: export+reimport of a 301-entry+1-sense glossary "
        f"(entries A={res_a['entries']} B={res_b['entries']}) stores match OK"
    )


def main():
    test_equivalence_live()
    test_equivalence_golden()
    test_batch_boundary_with_senses()
    test_no_insert_carries_id()
    test_csv_roundtrip_301_with_sense()
    test_scaling_linearity_and_budget()
    test_idempotent_reimport()
    print("GLOSSARY BULK IMPORT SCALING OK")


if __name__ == "__main__":
    main()
