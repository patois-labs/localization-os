"""tm.import_tmx STREAMING verification (upload-cap raise 25 -> 100 MiB) -- hermetic.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys, zero network.

Context: `config.MAX_UPLOAD_BYTES` moved from 25 MiB to 100 MiB (2026-07-27,
The maintainer's decision: the Microsoft Terminology Collection TBX is ~54 MB) because a
real termbase now legitimately exceeds the old cap. `tm.import_tmx` used to parse
the WHOLE TMX with `lxml.etree.fromstring` -- a full DOM materialization that is a
~1 GB peak-memory spike at 100 MB on the small production machine. It now
streams with stdlib `ET.iterparse`, clearing each `<tu>` (and its already-processed
siblings under `<body>`) as it goes, the same clear-as-you-go pattern
`glossary_tbx.parse_rows` already uses for TBX.

Covers:
  identity      -- the new streaming import_tmx produces the EXACT SAME counts and
                   the EXACT SAME resulting SegmentTM rows (source/target/hash/meta)
                   as a literal reimplementation of the OLD (pre-streaming, full-DOM)
                   algorithm, run side-by-side on an identical fixture. This is the
                   regression proof: if the streaming rewrite ever diverges from the
                   old semantics (locale fallback, refusal gate, meta round-trip,
                   in-file dedup, missing-pair skip), this test catches it.
  idempotent    -- re-importing the same bytes is a no-op (skipped); a changed
                   target on re-import updates in place (updated), matching the
                   pre-existing idempotency contract (bug #5).
  no-body       -- a well-formed TMX with no <body> (or a <body> that is not a
                   direct child of the root) returns the zero-count dict and writes
                   nothing -- unchanged from the old `root.find("body") is None`
                   short-circuit.
  malformed     -- truncated/invalid XML still raises (an Exception every caller's
                   bare `except Exception` still catches) and, because the streaming
                   loop's own `session.commit()` is never reached on that path,
                   nothing is left committed either -- same observable outcome as
                   the old whole-file parse raising before any DB write.
  memory-bounded-large-file -- a synthetically large (~60 MB) TMX, generated to a
                   file in the throwaway data dir and deleted immediately after,
                   imports correctly and completes in bounded wall-clock time
                   (previously this class of input is exactly the ~1 GB-DOM-spike
                   risk the streaming rewrite closes).

Adversarial-review regressions (2026-07-27), each FAILS on the first streaming cut:
  tu-selection  -- the identity fixture above is a well-formed TMX, so it cannot
                   detect the bug class the identity claim is actually about. This
                   diffs the SELECTION directly against the lxml oracle over 16
                   hostile shapes (two root <body> siblings, a nested <body> before
                   the real one, <tu>-in-<tu>, <tu> behind a wrapper, a namespaced
                   root, <body> as the root, comments/PIs, tails, CDATA, DTD default
                   attributes, UTF-16). Currently identical -- this keeps it so.
  entity-decl   -- the lxml -> ElementTree swap CHANGED the entity posture: libxml2
                   caps amplification at ~10x and rejected a 94x file outright,
                   while expat's ceiling is 100x and is not enforced below an 8 MiB
                   expansion. Measured on the pre-fix code: a 196 KB TMX expanded to
                   an 18 MB source_text and was COMMITTED to the TM (~10 GB at the
                   new 100 MiB cap). import_tmx now refuses a document declaring
                   entities, the same guard glossary_tbx._guard already applied to
                   TBX -- including the NUL-stripped pass for a UTF-16 declaration.
  size-cap      -- import_tmx enforces MAX_UPLOAD_BYTES itself. It is the ONLY cap
                   on the /api tm-import seam (base64 inside an unbounded JSON body)
                   and on the CLI's local-file read; only the web routes read capped.
  no-partial-in-session -- a malformed document late in the file used to leave the
                   speculatively-added rows PENDING in the caller's session, where
                   SQLAlchemy autoflushed them on the next query and any later
                   commit on that session persisted a partial import (reproduced:
                   2 rows). The exception path now undoes its own adds/mutations.
  batched-writes -- the bounded-memory pass (periodic flush + expunge inside a
                   SAVEPOINT, see verify_tm_import_bounded_memory.py) must be
                   INVISIBLE: same counts, same rows as the pre-batching full-DOM
                   oracle at every flush-boundary alignment, including the ones
                   that split the fixture's in-file duplicate pair.
  unmatched-doc-bounded -- an XML document the <body> latch never matches (a
                   NAMESPACED TMX, a body-less file, any other XML mis-uploaded to
                   the TM form) used to be cleared NEVER, so "streaming" silently
                   built the whole DOM: 17 MB file -> 37 MB Python heap (~2.1x),
                   i.e. ~210 MB at the 100 MiB cap. Now bounded (measured 0.1 MB).

Final line: "TM IMPORT STREAMING OK".
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

_TMP = init_env("loctms_tmstream_")

from lxml import etree as _lxml
from sqlalchemy import func
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Project, SegmentTM
from app.services import quality, tm
from app.services.tm import XML_LANG

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


# =============================================================================
# The OLD (pre-streaming) algorithm, reproduced verbatim as the regression oracle.
# This is NOT imported from app.services.tm (that code is gone) -- it is the exact
# full-DOM implementation import_tmx had before the iterparse rewrite, kept here
# ONLY so this test has an independent ground truth to diff the new streaming
# import_tmx's output against.
# =============================================================================
def _old_import_tmx(session, project_id, data, source_locale, target_locale) -> dict:
    root = _lxml.fromstring(data)
    body = root.find("body")
    if body is None:
        return {"imported": 0, "skipped": 0, "updated": 0, "skipped_refusal": 0}

    ct_id = tm._project_content_type_id(session, project_id)
    existing_by_hash = {
        row.source_hash: row
        for row in session.exec(
            select(SegmentTM).where(
                SegmentTM.project_id == project_id,
                SegmentTM.source_locale == source_locale,
                SegmentTM.target_locale == target_locale,
            )
        ).all()
    }

    imported = skipped = updated = skipped_refusal = 0
    added, changed = [], []
    for tu in body.findall("tu"):
        langs = {}
        for tuv in tu.findall("tuv"):
            lang = tuv.get(XML_LANG)
            seg = tuv.find("seg")
            text = "".join(seg.itertext()) if seg is not None else None
            if lang and text is not None:
                langs[lang] = text
        src = tm._pick(langs, source_locale)
        tgt = tm._pick(langs, target_locale)
        if not (src and tgt):
            continue
        if quality.is_refusal_check(tgt):
            skipped_refusal += 1
            continue
        h = tm.hash_text(src, source_locale, target_locale)
        existing = existing_by_hash.get(h)
        if existing is None:
            row = SegmentTM(
                project_id=project_id,
                source_locale=source_locale,
                target_locale=target_locale,
                source_text=src,
                target_text=tgt,
                source_hash=h,
                content_type_id=ct_id,
                meta=tm._meta_from_tu(tu),
            )
            session.add(row)
            added.append(row)
            existing_by_hash[h] = row
            imported += 1
        elif existing.target_text != tgt:
            existing.target_text = tgt
            session.add(existing)
            changed.append(existing)
            updated += 1
        else:
            skipped += 1
    session.commit()
    return {
        "imported": imported,
        "skipped": skipped,
        "updated": updated,
        "skipped_refusal": skipped_refusal,
    }


# =============================================================================
# fixture
# =============================================================================
def _rich_tmx() -> bytes:
    """A fixture exercising every branch import_tmx's streaming rewrite touches:
    exact-locale match, base-locale fallback (both sides), a missing-pair unit
    (skipped silently, no counter), a refusal hedge (skipped_refusal), a unit
    carrying a recognized x-* prop (meta round-trip), and an in-file duplicate
    source (the second occurrence dedupes against the first, imported once)."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4"><header srclang="{SRC}"/><body>
  <tu><tuv xml:lang="{SRC}"><seg>Hello world</seg></tuv>
      <tuv xml:lang="{TGT}"><seg>Bonjour le monde</seg></tuv></tu>
  <tu><tuv xml:lang="en"><seg>Base locale source</seg></tuv>
      <tuv xml:lang="fr"><seg>Source en locale de base</seg></tuv></tu>
  <tu><tuv xml:lang="{SRC}"><seg>Missing pair</seg></tuv></tu>
  <tu><tuv xml:lang="{SRC}"><seg>Hedge me</seg></tuv>
      <tuv xml:lang="{TGT}"><seg>{HEDGE}</seg></tuv></tu>
  <tu><prop type="x-origin">connector:jira</prop>
      <prop type="x-unknown-note">ignored</prop>
      <tuv xml:lang="{SRC}"><seg>Provenance carried</seg></tuv>
      <tuv xml:lang="{TGT}"><seg>Provenance conservee</seg></tuv></tu>
  <tu><tuv xml:lang="{SRC}"><seg>Repeated source</seg></tuv>
      <tuv xml:lang="{TGT}"><seg>Premiere version</seg></tuv></tu>
  <tu><tuv xml:lang="{SRC}"><seg>Repeated source</seg></tuv>
      <tuv xml:lang="{TGT}"><seg>Deuxieme version</seg></tuv></tu>
</body></tmx>""".encode()


def _rows(session, project_id):
    rows = session.exec(select(SegmentTM).where(SegmentTM.project_id == project_id)).all()
    return sorted(
        (r.source_text, r.target_text, r.source_hash, tuple(sorted((r.meta or {}).items())))
        for r in rows
    )


# =============================================================================
def test_streaming_matches_old_parser():
    data = _rich_tmx()
    with Session(engine) as s:
        p_old = _project(s, "OldParserOracle")
        p_new = _project(s, "NewStreamingImpl")

        res_old = _old_import_tmx(s, p_old.id, data, SRC, TGT)
        res_new = tm.import_tmx(s, p_new.id, data, SRC, TGT)

        # Counters only: import_tmx additionally returns the EFFECTIVE locales +
        # "locales_normalized" (QA BUG-03), which the old-parser oracle predates.
        assert _counters(res_old) == _counters(res_new), (res_old, res_new)
        assert (
            res_new["source_locale"],
            res_new["target_locale"],
            res_new["locales_normalized"],
        ) == (SRC, TGT, False), res_new
        # sanity on the numbers themselves (not just old==new): 3 imported
        # (Hello world, Base locale source via fallback, Provenance carried),
        # +1 for whichever of the repeated-source pair lands first = 4 imported,
        # 1 skipped_refusal (Hedge me), missing-pair unit silently skipped (no
        # counter), second "Repeated source" occurrence dedupes as "updated"
        # (same source_hash, different target_text -> updates the just-inserted row).
        assert _counters(res_new) == {
            "imported": 4,
            "skipped": 0,
            "updated": 1,
            "skipped_refusal": 1,
        }, res_new

        rows_old = _rows(s, p_old.id)
        rows_new = _rows(s, p_new.id)
        assert rows_old == rows_new, (rows_old, rows_new)
        # the meta round-trip landed (x-origin kept, x-unknown-note dropped).
        prov = next(r for r in rows_new if r[0] == "Provenance carried")
        assert prov[3] == (("origin", "connector:jira"),), prov
        # the base-locale fallback matched on BOTH sides.
        assert any(
            r[0] == "Base locale source" and r[1] == "Source en locale de base" for r in rows_new
        ), rows_new
        # the in-file duplicate source ends up as ONE row with the LAST target.
        dup = [r for r in rows_new if r[0] == "Repeated source"]
        assert len(dup) == 1 and dup[0][1] == "Deuxieme version", dup
    print(
        "  streaming import_tmx: identical counts + identical resulting rows "
        "to the old full-DOM parser (locale fallback, refusal gate, meta "
        "round-trip, missing-pair skip, in-file dedup) OK"
    )


def test_batched_writes_match_old_parser_at_every_flush_boundary():
    """The bounded-memory pass (2026-07-27) made import_tmx flush + EXPUNGE every
    tm.TMX_IMPORT_FLUSH_BATCH units inside a SAVEPOINT instead of accumulating ORM
    state until one commit. That is a pure memory change: counts and stored rows
    must stay IDENTICAL to the pre-streaming, pre-batching full-DOM oracle above,
    at EVERY batch size -- including the ones that split _rich_tmx's in-file
    duplicate pair across a flush, so the second occurrence resolves against a row
    that is already written to the DB and no longer in the session. (That case is
    the one a batching bug lands on: a duplicated INSERT, or an update applied to
    an expunged object that silently never reaches the DB.)"""
    data = _rich_tmx()
    original = tm.TMX_IMPORT_FLUSH_BATCH
    try:
        with Session(engine) as s:
            oracle_p = _project(s, "BatchOracle")
            oracle_res = _old_import_tmx(s, oracle_p.id, data, SRC, TGT)
            oracle_rows = _rows(s, oracle_p.id)
            for batch in (1, 2, 3, 4, 6, 8, original):
                set_pieces("app.services.tm", "TMX_IMPORT_FLUSH_BATCH", batch)
                p = _project(s, f"BatchNew-{batch}")
                res = tm.import_tmx(s, p.id, data, SRC, TGT)
                assert _counters(res) == _counters(oracle_res), (batch, res, oracle_res)
                assert _rows(s, p.id) == oracle_rows, (batch, _rows(s, p.id))
                # and the duplicate is ONE row carrying the LAST target, however
                # the flush boundaries fell across the file
                dup = [r for r in _rows(s, p.id) if r[0] == "Repeated source"]
                assert len(dup) == 1 and dup[0][1] == "Deuxieme version", (batch, dup)
    finally:
        set_pieces("app.services.tm", "TMX_IMPORT_FLUSH_BATCH", original)
    print(
        "  streaming import_tmx: batched flush+expunge writes are identical to "
        "the pre-batching full-DOM oracle at batch sizes 1/2/3/4/6/8/default "
        "(in-file duplicate straddling a flush included) OK"
    )


def _simple_tmx() -> bytes:
    """A duplicate-free fixture (unlike _rich_tmx, which deliberately carries an
    in-file duplicate source whose stored target OSCILLATES on every reimport --
    real behavior, exercised by test_streaming_matches_old_parser, but noise for
    a plain idempotency check) so re-import counts are unambiguous."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4"><header srclang="{SRC}"/><body>
  <tu><tuv xml:lang="{SRC}"><seg>Hello world</seg></tuv>
      <tuv xml:lang="{TGT}"><seg>Bonjour le monde</seg></tuv></tu>
  <tu><tuv xml:lang="{SRC}"><seg>Save changes</seg></tuv>
      <tuv xml:lang="{TGT}"><seg>Enregistrer les modifications</seg></tuv></tu>
  <tu><tuv xml:lang="{SRC}"><seg>Hedge me</seg></tuv>
      <tuv xml:lang="{TGT}"><seg>{HEDGE}</seg></tuv></tu>
</body></tmx>""".encode()


def test_streaming_idempotent_reimport():
    data = _simple_tmx()
    with Session(engine) as s:
        p = _project(s, "Idempotent")
        first = tm.import_tmx(s, p.id, data, SRC, TGT)
        assert _counters(first) == {
            "imported": 2,
            "skipped": 0,
            "updated": 0,
            "skipped_refusal": 1,
        }, first

        second = tm.import_tmx(s, p.id, data, SRC, TGT)
        # byte-identical re-import: both rows already match -> a true no-op.
        assert _counters(second) == {
            "imported": 0,
            "skipped": 2,
            "updated": 0,
            "skipped_refusal": 1,
        }, second

        changed = data.replace(b"Bonjour le monde", b"Salut le monde")
        third = tm.import_tmx(s, p.id, changed, SRC, TGT)
        assert _counters(third) == {
            "imported": 0,
            "skipped": 1,
            "updated": 1,
            "skipped_refusal": 1,
        }, third
        row = s.exec(
            select(SegmentTM).where(
                SegmentTM.project_id == p.id, SegmentTM.source_text == "Hello world"
            )
        ).first()
        assert row.target_text == "Salut le monde", row.target_text
    print(
        "  streaming import_tmx: idempotent re-import (skipped) + changed "
        "target re-import (updated) OK"
    )


def test_streaming_no_body_and_malformed():
    with Session(engine) as s:
        p1 = _project(s, "NoBody")
        no_body = b'<?xml version="1.0"?><tmx version="1.4"><header/></tmx>'
        res = tm.import_tmx(s, p1.id, no_body, SRC, TGT)
        assert _counters(res) == {
            "imported": 0,
            "skipped": 0,
            "updated": 0,
            "skipped_refusal": 0,
        }, res
        assert _rows(s, p1.id) == [], "a body-less TMX must write nothing"

        p2 = _project(s, "NestedBody")
        # <body> nested under an extra wrapper is NOT a direct child of the
        # document root -- root.find("body") would have returned None for this,
        # same zero-result contract as the no-body case.
        nested = (
            b'<?xml version="1.0"?><tmx version="1.4"><wrapper><body>'
            b'<tu><tuv xml:lang="en-US"><seg>x</seg></tuv>'
            b'<tuv xml:lang="fr-FR"><seg>y</seg></tuv></tu>'
            b"</body></wrapper></tmx>"
        )
        res2 = tm.import_tmx(s, p2.id, nested, SRC, TGT)
        assert _counters(res2) == {
            "imported": 0,
            "skipped": 0,
            "updated": 0,
            "skipped_refusal": 0,
        }, res2
        assert _rows(s, p2.id) == [], "a non-root-child <body> must write nothing"

        p3 = _project(s, "Malformed")
        malformed = b'<?xml version="1.0"?><tmx version="1.4"><body><tu>'  # truncated
        raised = False
        try:
            tm.import_tmx(s, p3.id, malformed, SRC, TGT)
        except Exception:
            raised = True
        assert raised, "malformed XML must raise (callers rely on this)"
        assert _rows(s, p3.id) == [], "a failed import must leave nothing committed"
    print(
        "  streaming import_tmx: no-body / non-root-child-body -> zero-result "
        "no-op; malformed XML -> raises with nothing committed OK"
    )


def test_streaming_memory_bounded_large_file():
    """A synthetic ~60 MB TMX (well past the OLD 25 MiB cap, comfortably under the
    NEW 100 MiB cap) must import correctly without materializing a full DOM tree.
    Generated to a file in the throwaway data dir and deleted right after, per the
    house rule against leaving test debris around. No RSS assertion (no psutil /
    resource module portably available here) -- completion within a generous
    wall-clock bound plus exact-count correctness is the achievable proof; the
    thing being guarded against (an O(file-size) DOM multiplying into a ~1 GB
    spike) would show up as either a MemoryError or a many-minutes stall, not a
    subtly wrong count."""
    n = 5000
    pad_src = "x" * 6000
    pad_tgt = "y" * 6000
    parts = [
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<tmx version="1.4"><header srclang="{SRC}"/><body>\n'
    ]
    for i in range(n):
        parts.append(
            f'<tu><tuv xml:lang="{SRC}"><seg>Row {i} {pad_src}</seg></tuv>'
            f'<tuv xml:lang="{TGT}"><seg>Fila {i} {pad_tgt}</seg></tuv></tu>\n'
        )
    parts.append("</body></tmx>")
    blob = "".join(parts).encode("utf-8")
    size_mb = len(blob) / (1024 * 1024)
    assert size_mb > 45, f"fixture too small to be a meaningful stress case: {size_mb:.1f} MB"

    path = os.path.join(_TMP, "synthetic_large.tmx")
    with open(path, "wb") as fh:
        fh.write(blob)
    del blob
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        with Session(engine) as s:
            p = _project(s, "LargeFile")
            t0 = time.monotonic()
            res = tm.import_tmx(s, p.id, data, SRC, TGT)
            elapsed = time.monotonic() - t0
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
        # Generous: this is about proving "completes at all" (bounded memory),
        # not benchmarking; a regression back to full-DOM parsing would either
        # blow memory or run dramatically slower than this on any real machine.
        assert elapsed < 120, f"streaming import of a {size_mb:.0f} MB TMX took {elapsed:.1f}s"
    finally:
        os.remove(path)
    print(
        f"  streaming import_tmx: {size_mb:.0f} MB / {n} units imported correctly "
        f"in {elapsed:.1f}s without materializing a full DOM tree, temp file removed OK"
    )


def test_tu_selection_matches_old_parser_on_hostile_shapes():
    """The identity fixture above is a well-formed TMX -- it cannot detect the
    class of bug the identity claim is really about: the streaming latch reaching
    a <tu> that `root.find("body").findall("tu")` would NOT have (a second root
    <body>, a <tu> nested in another <tu> or behind a wrapper, a namespaced root),
    or vice versa. Diff the two SELECTIONS directly on those shapes, using the
    same lxml full-DOM oracle."""

    def old_selection(data):
        root = _lxml.fromstring(data)
        body = root.find("body")
        if body is None:
            return None
        return ["".join(t.itertext()).split() for t in body.findall("tu")]

    def new_selection(data):
        """The SHIPPED loop's tu-selection + clear discipline, with the DB work
        replaced by a text capture (kept in lockstep with tm.import_tmx)."""
        import io as _io
        import xml.etree.ElementTree as _ET

        root_body = open_tu = None
        stack, out = [], []
        for event, elem in _ET.iterparse(_io.BytesIO(data), events=("start", "end")):
            if event == "start":
                parent = stack[-1] if stack else None
                if elem.tag == "body" and root_body is None and len(stack) == 1:
                    root_body = elem
                elif (
                    elem.tag == "tu"
                    and open_tu is None
                    and parent is not None
                    and parent is root_body
                ):
                    open_tu = elem
                stack.append(elem)
                continue
            stack.pop()
            parent = stack[-1] if stack else None
            if elem is not open_tu:
                if open_tu is None:
                    elem.clear()
                    if parent is not None:
                        parent.clear()
                continue
            open_tu = None
            out.append("".join(elem.itertext()).split())
            elem.clear()
            if stack:
                stack[-1].clear()
        return None if root_body is None else out

    cases = {
        "two root <body> siblings": b"<tmx><body><tu>A</tu></body><body><tu>B</tu></body></tmx>",
        "nested <body> before the real one": b"<tmx><header><body><tu>N</tu></body></header><body><tu>R</tu></body></tmx>",  # noqa: E501
        "<tu> nested in a <tu>": b"<tmx><body><tu>OUT<tu>IN</tu></tu><tu>Z</tu></body></tmx>",
        "<tu> nested deeper in a <tu>": b"<tmx><body><tu>O<x><tu>I</tu></x></tu><tu>Z</tu></body></tmx>",  # noqa: E501
        "<tu> behind a wrapper in <body>": b"<tmx><body><wrap><tu>W</tu></wrap><tu>D</tu></body></tmx>",  # noqa: E501
        "namespaced root": b'<tmx xmlns="http://www.lisa.org/tmx14"><body><tu>A</tu></body></tmx>',
        "<body> IS the root": b"<body><tu>A</tu></body>",
        "a second <body> after a trailer element": b"<tmx><body><tu>A</tu></body><trailer><body><tu>X</tu></body></trailer></tmx>",  # noqa: E501
        "comments + PIs between units": b"<tmx><body><!--c--><tu>A</tu><?pi x?><tu>B</tu></body></tmx>",  # noqa: E501
        "text tails around units": b"<tmx><body>t0<tu>A</tu>t1<tu>B</tu>t2</body></tmx>",
        "CDATA + tail inside a unit": b"<tmx><body><tu><![CDATA[C1]]>tail</tu></body></tmx>",
        "non-<tu> siblings in <body>": b"<tmx><body><tu>A</tu><note>n</note><tu>B</tu></body></tmx>",  # noqa: E501
        "empty <tu>": b"<tmx><body><tu/><tu>B</tu></body></tmx>",
        "empty <body>": b"<tmx><body/></tmx>",
        "DTD default attributes": b'<?xml version="1.0"?><!DOCTYPE tmx [<!ATTLIST tu id CDATA "z">]>'  # noqa: E501
        b"<tmx><body><tu>A</tu></body></tmx>",
        "UTF-16 encoded": '<?xml version="1.0" encoding="UTF-16"?><tmx><body><tu>Uni</tu></body></tmx>'.encode(  # noqa: E501
            "utf-16"
        ),
    }
    for label, data in cases.items():
        assert old_selection(data) == new_selection(data), label
    print(
        f"  streaming import_tmx: tu SELECTION identical to root.find('body')"
        f".findall('tu') across {len(cases)} hostile document shapes "
        f"(multi-body, nested tu, wrapper, namespace, UTF-16, DTD) OK"
    )


def _entity_tmx(encoding="utf-8") -> bytes:
    """A TMX whose <seg> is one entity reference expanding 90x. Under expat this
    parses and stores a multi-MB source_text; under the old lxml parser libxml2's
    non-linear-amplification limit rejected it. Neither may happen now: the
    declaration itself is the refusal trigger."""
    doc = (
        '<?xml version="1.0"?>\n<!DOCTYPE tmx [\n<!ENTITY big "{}">\n]>\n'
        '<tmx version="1.4"><header srclang="{}"/><body><tu>'
        '<tuv xml:lang="{}"><seg>{}</seg></tuv>'
        '<tuv xml:lang="{}"><seg>cible</seg></tuv></tu></body></tmx>'.format(
            "A" * 20_000, SRC, SRC, "&big;" * 90, TGT
        )
    )
    return doc.encode(encoding)


def test_entity_declaration_refused():
    with Session(engine) as s:
        p = _project(s, "EntityDecl")
        for label, data in (
            ("utf-8", _entity_tmx()),
            # UTF-16 interleaves NULs so the raw bytes never spell
            # "<!ENTITY", but expat auto-detects the BOM and expands
            # the entity identically -- the NUL-stripped pass catches it.
            ("utf-16", _entity_tmx("utf-16")),
        ):
            raised = None
            try:
                tm.import_tmx(s, p.id, data, SRC, TGT)
            except Exception as exc:
                raised = exc
            assert raised is not None, f"{label} entity-declaring TMX must be refused"
            assert "entities" in str(raised), (label, str(raised))
        assert _rows(s, p.id) == [], "an entity-declaring TMX must write nothing"

        # A file with no entity declaration is untouched by the guard.
        ok = tm.import_tmx(s, p.id, _simple_tmx(), SRC, TGT)
        assert ok["imported"] == 2, ok
    print(
        "  streaming import_tmx: entity-declaring TMX refused (utf-8 AND utf-16, "
        "the amplification vector the lxml->ElementTree swap opened) OK"
    )


def test_size_cap_enforced_in_service():
    """MAX_UPLOAD_BYTES is enforced by import_tmx itself, not only by the web
    routes' read_capped_upload -- /api tm-import base64-decodes an unbounded JSON
    body and the CLI reads a local file whole, so this is their only cap."""
    from app import config as appcfg

    orig = appcfg.MAX_UPLOAD_BYTES
    appcfg.MAX_UPLOAD_BYTES = 256
    try:
        with Session(engine) as s:
            p = _project(s, "SizeCap")
            raised = None
            try:
                tm.import_tmx(s, p.id, _simple_tmx(), SRC, TGT)
            except Exception as exc:
                raised = exc
            assert raised is not None, "an over-cap TMX must be refused"
            assert "upload limit" in str(raised), str(raised)
            assert _rows(s, p.id) == [], "an over-cap TMX must write nothing"
    finally:
        appcfg.MAX_UPLOAD_BYTES = orig
    print(
        "  streaming import_tmx: MAX_UPLOAD_BYTES enforced in the service seam "
        "(the only cap /api tm-import and the CLI have) OK"
    )


def test_failed_import_leaves_session_clean():
    """A document that goes malformed AFTER some units parsed must not leave those
    rows pending in the caller's session: SQLAlchemy autoflushes them on the next
    query and any later commit on that session would persist a PARTIAL import."""
    good_then_broken = (
        '<?xml version="1.0"?><tmx version="1.4"><body>'
        f'<tu><tuv xml:lang="{SRC}"><seg>Alpha</seg></tuv>'
        f'<tuv xml:lang="{TGT}"><seg>Alpha-FR</seg></tuv></tu>'
        f'<tu><tuv xml:lang="{SRC}"><seg>Beta</seg></tuv>'
        f'<tuv xml:lang="{TGT}"><seg>Beta-FR</seg></tuv></tu>'
        f'<tu><tuv xml:lang="{SRC}"><seg>Gamma'
    ).encode()  # truncated
    with Session(engine) as s:
        p = _project(s, "SessionClean")
        # seed one row so the UPDATE branch is exercised on the failing import too
        tm.import_tmx(
            s,
            p.id,
            (
                '<?xml version="1.0"?><tmx version="1.4"><body>'
                f'<tu><tuv xml:lang="{SRC}"><seg>Alpha</seg></tuv>'
                f'<tuv xml:lang="{TGT}"><seg>SEEDED</seg></tuv></tu>'
                "</body></tmx>"
            ).encode(),
            SRC,
            TGT,
        )
        try:  # noqa: SIM105
            tm.import_tmx(s, p.id, good_then_broken, SRC, TGT)
        except Exception:
            pass
        # a follow-up query on the SAME session autoflushes anything left pending
        rows = _rows(s, p.id)
        assert [r[0] for r in rows] == ["Alpha"], rows
        assert rows[0][1] == "SEEDED", "the failed import's UPDATE must be undone too"
        s.commit()  # any later unit of work on the same session
        assert [r[0] for r in _rows(s, p.id)] == ["Alpha"], _rows(s, p.id)
    with Session(engine) as s2:
        p2 = s2.exec(select(Project).where(Project.name == "SessionClean")).first()
        rows = _rows(s2, p2.id)
        assert [(r[0], r[1]) for r in rows] == [("Alpha", "SEEDED")], rows
    print(
        "  streaming import_tmx: a mid-file parse failure leaves the caller's "
        "session clean (no autoflushed partial import) OK"
    )


def test_unmatched_document_is_still_streamed():
    """A document whose root-level <body> latch never matches -- a NAMESPACED TMX
    is the realistic one -- must STILL be cleared as it is walked. Before the fix
    nothing was ever cleared for this class, so the whole DOM was materialized
    (17 MB file -> 37 MB Python heap) at exactly the sizes the 100 MiB cap now
    admits. Asserted on traced Python-heap peak, which is deterministic here."""
    import gc
    import tracemalloc

    n, pad = 4000, "x" * 400
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<tmx version="1.4" xmlns="http://www.lisa.org/tmx14"><header/><body>'
    ]
    for i in range(n):
        parts.append(
            f'<tu><tuv xml:lang="{SRC}"><seg>Row {i} {pad}</seg></tuv>'
            f'<tuv xml:lang="{TGT}"><seg>Fila {i} {pad}</seg></tuv></tu>'
        )
    parts.append("</body></tmx>")
    data = "".join(parts).encode("utf-8")
    del parts
    with Session(engine) as s:
        p = _project(s, "NamespacedLarge")
        gc.collect()
        tracemalloc.start()
        res = tm.import_tmx(s, p.id, data, SRC, TGT)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    # a namespaced <body> is not the plain "body" tag the old root.find("body")
    # matched, so the zero-result contract is unchanged...
    assert _counters(res) == {"imported": 0, "skipped": 0, "updated": 0, "skipped_refusal": 0}, res
    # ...but walking it must no longer cost a full DOM. Pre-fix this was ~2.1x the
    # file; the bound is deliberately loose (a fifth of the file) so it can only
    # fire on a real regression back to tree-building, never on allocator noise.
    assert peak < len(data) // 5, (
        f"unmatched document built a tree: peak {peak / 1e6:.1f} MB for a "
        f"{len(data) / 1e6:.1f} MB file"
    )
    print(
        f"  streaming import_tmx: a namespaced (never-latched) {len(data) / 1e6:.0f} MB "
        f"TMX walks in {peak / 1e6:.2f} MB of Python heap, no full DOM OK"
    )


if __name__ == "__main__":
    init_db()
    test_streaming_matches_old_parser()
    test_batched_writes_match_old_parser_at_every_flush_boundary()
    test_tu_selection_matches_old_parser_on_hostile_shapes()
    test_streaming_idempotent_reimport()
    test_streaming_no_body_and_malformed()
    test_entity_declaration_refused()
    test_size_cap_enforced_in_service()
    test_failed_import_leaves_session_clean()
    test_unmatched_document_is_still_streamed()
    test_streaming_memory_bounded_large_file()
    print("TM IMPORT STREAMING OK")
