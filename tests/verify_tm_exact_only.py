"""TM fill is exact-only (100 % and in-context 101 %), never misses, and stays fast at scale (ADR 0102).

Promoted from the architecture target T5 (2026-10-22). With a
TM of LOC_TMS_T5_ROWS units (default 200,000):

  1. exact recall: every query equal to a TM source (after normalization) gets a 100 % hit, 500/500;
  2. in-context: a unit stored with the same previous / next source ranks 101 % and wins over a
     context-free 100 % (the TMX import takes the adjacent units in file order as the context);
  3. latency: at most 2 ms per segment (p95, batches of 50);
  4. fuzzy is gone: no rapidfuzz in tm / tm_hub, min_score below 100 raises, a source one word away
     from a stored one gets NO hit and nothing fills from it.

Then the smaller cases that pin the rest of the design: 101 versus 100 ranking with an attached
source project, the same source twice in one document with different neighbours (lookup_segments),
write_back storing the context on insert AND update, a TMX round trip keeping context_key and the
Trados-style props, the one-time backfill (the alembic revision's function) setting keys from
approved segments, string-key (software) projects matching on the key name, and the lock plan for
a 101 hit.

ASCII-only output.
"""
# ruff: noqa: E501

import importlib.util
import os
import random
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_t5_")

from sqlmodel import Session, select

from app.db import engine, init_db

init_db()
from app.models import (
    Customer,
    Document,
    Project,
    Segment,
    SegmentTM,
    SegStatus,
    StringKey,
    TargetLocale,
)
from app.services import project_sources, tm, tm_common, tm_context, tm_hub, tm_lock

ROWS = int(os.environ.get("LOC_TMS_T5_ROWS", "200000"))
SRC, TGT = "en-US", "es-ES"
rng = random.Random(5)
PRODUCTS = [
    "Ridgeline 45",
    "Ember 2 shelter",
    "Larch fleece",
    "Summit 30 pack",
    "Cascade rain shell",
    "Alpine stove",
    "Talus boots",
    "Drift sleeping bag",
    "Kestrel headlamp",
    "Basin water filter",
]
ADJ = ["light", "warm", "tough", "compact", "waterproof", "breathable", "quiet", "fast"]
SYL = [
    "ka",
    "lo",
    "mer",
    "tan",
    "vi",
    "sor",
    "el",
    "dun",
    "ra",
    "bel",
    "quin",
    "os",
    "ta",
    "wen",
    "har",
    "li",
]
TPL = [
    "The {p} is {a} enough for trips on {pl}.",
    "Save {n}% on the {p} this week.",
    "We tested the {p} for {n} days on {pl}.",
    "Pack the {p} in the lid so it stays {a}.",
    "Free shipping on orders over ${n} near {pl}.",
    "Ask our guides about the {p} on live chat.",
]


def sentence():
    pl = "".join(rng.choice(SYL) for _ in range(rng.randint(2, 3))).capitalize() + " Trail"
    return rng.choice(TPL).format(
        p=rng.choice(PRODUCTS), a=rng.choice(ADJ), pl=pl, n=rng.randint(2, 400)
    )


def mkproject(name):
    with Session(engine) as s:
        cust = Customer(name=f"{name} Co")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        proj = Project(name=name, customer_id=cust.id, source_locale=SRC, target_locales=TGT)
        s.add(proj)
        s.commit()
        s.refresh(proj)
        return proj.id, cust.id


def pct(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(q * len(vals)))] if vals else None


# --------------------------------------------------------------------------------------------
# the big TM (T5): built from a TMX file, in file order, so adjacent units give each other context
# --------------------------------------------------------------------------------------------
def build():
    srcs, seen = [], set()
    path = os.path.join(_TMP, "t5.tmx")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            '<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header creationtool="t5" '
            'creationtoolversion="1" segtype="sentence" o-tmf="t5" adminlang="en" srclang="en-US" '
            'datatype="plaintext"/><body>'
        )
        while len(srcs) < ROWS:
            en = f"{sentence()} {sentence()}"
            if en in seen:
                continue
            seen.add(en)
            srcs.append(en)
            fh.write(
                f'<tu><tuv xml:lang="en-US"><seg>{en}</seg></tuv><tuv xml:lang="es-ES"><seg>ES {en}</seg>'
                f"</tuv></tu>"
            )
        fh.write("</body></tmx>")
    pid, _cid = mkproject("t5")
    t0 = time.time()
    with Session(engine) as s:
        tm.import_tmx_file(s, pid, path, SRC, TGT)
    print(f"  built TM: {ROWS} units in {time.time() - t0:.0f}s")
    return pid, srcs


PID, SRCS = build()


def lookup(batch, **kw):
    with Session(engine) as s:
        return tm_hub.lookup_batch(s, PID, batch, SRC, TGT, min_score=100, **kw)


def test_exact_recall():
    qs = rng.sample(SRCS, 500)
    hits = 0
    for i in range(0, len(qs), 50):
        got = lookup(qs[i : i + 50])
        hits += sum(
            1 for q in qs[i : i + 50] if int((got.get(q) or {}).get("match_pct") or 0) >= 100
        )
    assert hits == 500, f"{hits}/500 exact queries got a hit"
    # whitespace-normalised queries match too (hash_text normalizes)
    q = "  " + SRCS[3].replace(" ", "  ", 1) + " "
    assert lookup([q])[q]["match_pct"] >= 100
    print(f"  1 exact recall: {hits}/500 exact queries got a 100 % hit")
    return hits


def test_in_context_on_import_adjacency():
    q = SRCS[7]
    got = lookup([q], contexts={q: (SRCS[6], SRCS[8])})
    assert got[q]["match_pct"] == 101, got[q]
    # wrong neighbours: still a plain 100
    got = lookup([q], contexts={q: (SRCS[1], SRCS[2])})
    assert got[q]["match_pct"] == 100, got[q]
    # the file edges: first unit has an empty previous, last an empty next
    first = lookup([SRCS[0]], contexts={SRCS[0]: ("", SRCS[1])})[SRCS[0]]
    last = lookup([SRCS[-1]], contexts={SRCS[-1]: (SRCS[-2], "")})[SRCS[-1]]
    assert first["match_pct"] == 101 and last["match_pct"] == 101, (first, last)
    # a context-free query is a 100 and reports the stored key
    plain = lookup([q])[q]
    assert plain["match_pct"] == 100 and plain["context_key"] == tm_common.context_key(
        SRCS[6], SRCS[8]
    )
    print(
        "  2 in-context: 101 with the file-order neighbours, 100 with others, edges are empty strings"
    )


def test_latency():
    per = []
    for _ in range(10):
        b = rng.sample(SRCS, 50)
        t0 = time.perf_counter()
        lookup(b)
        per.append((time.perf_counter() - t0) / 50 * 1000)
    p95 = pct(per, 0.95)
    assert p95 <= 2.0, f"exact lookup p95 {p95:.2f} ms per segment at {ROWS} units (limit 2 ms)"
    print(f"  3 latency: p95 {p95:.2f} ms per segment at {ROWS} units (limit 2 ms)")
    return p95


def test_fuzzy_removed():
    with open(os.path.join(_ROOT, "app", "services", "tm_hub.py"), encoding="utf-8") as fh:
        src_text = fh.read()
    with open(os.path.join(_ROOT, "app", "services", "tm.py"), encoding="utf-8") as fh:
        src_tm = fh.read()
    assert "rapidfuzz" not in src_text.replace("rapidfuzz stays", "")
    assert (
        "import fuzz" not in src_tm
        and "rapidfuzz" not in src_tm.split('"""', 2)[2].split("import")[0]
    )
    for name in (
        "fuzz",
        "_fuzzy_ratio",
        "TM_FUZZY_MAX_CHARS",
        "TM_FUZZY_CANDIDATE_CAP",
        "TM_UNION_CHUNK",
        "example_matches_batch",
    ):
        assert not hasattr(tm_hub, name) and not hasattr(tm, name), f"{name} still exists"
    q = SRCS[11]
    for bad in (95, 75, 1, 99):
        try:
            lookup_bad = tm_hub.lookup_batch
            with Session(engine) as s:
                lookup_bad(s, PID, [q], SRC, TGT, min_score=bad)
        except ValueError as exc:
            assert "ADR 0102" in str(exc)
        else:
            raise AssertionError(f"min_score={bad} did not raise")
    with Session(engine) as s:
        try:
            tm_hub.lookup(s, PID, q, SRC, TGT, min_score=90)
        except ValueError:
            pass
        else:
            raise AssertionError("lookup(min_score=90) did not raise")
    # one word changed -> no hit
    words = q.split(" ")
    near = " ".join([*words[:2], "zzzword", *words[3:]])
    assert lookup([near])[near] is None
    print(
        "  4 fuzzy removed: no rapidfuzz in tm/tm_hub, min_score<100 raises, one-word-off gets no hit"
    )


def test_near_identical_does_not_fill():
    """A document whose segment differs by one word from a stored unit stays unfilled by apply-tm."""
    pid, _ = mkproject("near")
    with Session(engine) as s:
        tm.write_back(
            s, pid, "Click the green button to continue.", "Haga clic en el boton verde.", SRC, TGT
        )
        doc = Document(project_id=pid, filename="n.txt", fmt="txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(TargetLocale(project_id=pid, locale=TGT))
        s.add(
            Segment(
                document_id=doc.id,
                target_locale=TGT,
                order_index=0,
                source_text="Click the red button to continue.",
            )
        )
        s.add(
            Segment(
                document_id=doc.id,
                target_locale=TGT,
                order_index=1,
                source_text="Click the green button to continue.",
            )
        )
        s.commit()
        from app.services import content

        proj = s.get(Project, pid)
        n = content.apply_tm_to_document(s, doc, proj, TGT)
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc.id).order_by(Segment.order_index)
        ).all()
    assert n == 1, n
    assert segs[0].target_text == "" and segs[0].tm_match_pct is None
    assert segs[1].target_text.startswith("Haga clic") and segs[1].tm_match_pct in (100, 101)
    print("  4b near-identical source (one word off) is not filled by apply-tm; the exact one is")


# --------------------------------------------------------------------------------------------
# ranking: 101 vs 100 with an attached source project
# --------------------------------------------------------------------------------------------
def test_101_vs_100_with_attached_source():
    own, cid = mkproject("own")
    with Session(engine) as s:
        # a second project of the SAME customer to attach as a TM source
        other = Project(name="src", customer_id=cid, source_locale=SRC, target_locales=TGT)
        s.add(other)
        s.commit()
        s.refresh(other)
        oid = other.id
        project_sources.add_tm_source(s, own, oid)
        # own row: context-free; source project row: with context K and a DIFFERENT target
        tm.write_back(s, own, "Open the lid.", "Abra la tapa (propio).", SRC, TGT)
        tm.write_back(
            s,
            oid,
            "Open the lid.",
            "Abra la tapa (fuente).",
            SRC,
            TGT,
            context=("Before.", "After."),
        )
        k = tm_common.context_key("Before.", "After.")
        hit_ctx = tm_hub.lookup_segments(s, own, [("Open the lid.", k)], SRC, TGT)[0]
        hit_none = tm_hub.lookup_segments(s, own, [("Open the lid.", None)], SRC, TGT)[0]
        # M1: the project's own row is never shadowed by a borrowed in-context row with a
        # different target: own wins, at 100
        assert hit_ctx["match_pct"] == 100 and hit_ctx["target_text"].endswith("(propio)."), hit_ctx
        assert hit_none["match_pct"] == 100 and hit_none["target_text"].endswith("(propio).")
        # the borrowed row holds the SAME target in that context: it confirms the own row -> 101
        tm.write_back(
            s,
            oid,
            "Open the lid.",
            "Abra la tapa (propio).",
            SRC,
            TGT,
            context=("Before.", "After."),
        )
        same = tm_hub.lookup_segments(s, own, [("Open the lid.", k)], SRC, TGT)[0]
        assert same["match_pct"] == 101 and same["target_text"].endswith("(propio)."), same
        # a different query context stays 100
        other_ctx = tm_hub.lookup_segments(
            s, own, [("Open the lid.", tm_common.context_key("x", "y"))], SRC, TGT
        )[0]
        assert other_ctx["match_pct"] == 100 and other_ctx["target_text"].endswith("(propio).")
        # own row in context itself -> 101
        tm.write_back(
            s,
            own,
            "Open the lid.",
            "Abra la tapa (propio ctx).",
            SRC,
            TGT,
            context=("Before.", "After."),
        )
        tie = tm_hub.lookup_segments(s, own, [("Open the lid.", k)], SRC, TGT)[0]
        assert tie["match_pct"] == 101 and tie["target_text"].endswith("(propio ctx)."), tie
        # no own row: an attached in-context row is a 101, any other query is the 100
        tm.write_back(
            s, oid, "Close the lid.", "Cierre la tapa.", SRC, TGT, context=("Before.", "After.")
        )
        borrowed = tm_hub.lookup_segments(s, own, [("Close the lid.", k)], SRC, TGT)[0]
        assert borrowed["match_pct"] == 101 and borrowed["target_text"] == "Cierre la tapa."
        plain = tm_hub.lookup_segments(s, own, [("Close the lid.", None)], SRC, TGT)[0]
        assert plain["match_pct"] == 100
        # the single-source wrapper agrees
        one = tm_hub.lookup(s, own, "Open the lid.", SRC, TGT, context=("Before.", "After."))
        assert one["match_pct"] == 101
    print(
        "  5 101 vs 100: own row is never shadowed by a borrowed in-context row; same target "
        "promotes it; no own row: borrowed in-context is 101"
    )


def test_same_source_twice_different_neighbours():
    pid, _ = mkproject("twice")
    with Session(engine) as s:
        doc = Document(project_id=pid, filename="t.txt", fmt="txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        texts = ["Intro.", "Save", "Middle text.", "Other.", "Save", "Closing."]
        for i, t in enumerate(texts):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=TGT,
                    order_index=i,
                    source_text=t,
                    target_text="Guardar" if t == "Save" else f"T{i}",
                    status=SegStatus.APPROVED if i == 1 else SegStatus.NEW,
                )
            )
        s.commit()
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc.id).order_by(Segment.order_index)
        ).all()
        # approving segment 1 writes the unit with ITS neighbours (Intro., Middle text.)
        assert tm.write_back(
            s, pid, segs[1].source_text, segs[1].target_text, SRC, TGT, segment=segs[1]
        )
        keys = tm_context.segment_context_keys(s, segs)
        hits = tm_hub.lookup_segments(
            s, pid, [(sg.source_text, k) for sg, k in zip(segs, keys, strict=True)], SRC, TGT
        )
        assert hits[1]["match_pct"] == 101, hits[1]
        assert hits[4]["match_pct"] == 100, hits[4]  # same source, different neighbours
        assert hits[0] is None and hits[2] is None
        # lookup_batch is keyed by source: it cannot tell the two apart (documented), lookup_segments can
        assert tm_hub.lookup_batch(s, pid, ["Save"], SRC, TGT)["Save"]["match_pct"] == 100
    print(
        "  6 same source twice in a document: 101 for the approved context, 100 for the other (positional)"
    )


def test_write_back_stores_context_on_insert_and_update():
    pid, _ = mkproject("wb")
    with Session(engine) as s:
        doc = Document(project_id=pid, filename="w.txt", fmt="txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, t in enumerate(["Alpha one.", "Beta two.", "Gamma three."]):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=TGT,
                    order_index=i,
                    source_text=t,
                    target_text=f"ES {t}",
                )
            )
        s.commit()
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc.id).order_by(Segment.order_index)
        ).all()
        row = tm.write_back(s, pid, segs[1].source_text, "ES Beta two.", SRC, TGT, segment=segs[1])
        assert row.context_key == tm_common.context_key("Alpha one.", "Gamma three.")
        # document edge: first segment has an empty previous
        row0 = tm.write_back(
            s, pid, segs[0].source_text, "ES Alpha one.", SRC, TGT, segment=segs[0]
        )
        assert row0.context_key == tm_common.context_key("", "Beta two.")
        # UPDATE arm: neighbours change, the same row takes the new context
        segs[2].source_text = "Gamma changed."
        s.add(segs[2])
        s.commit()
        row2 = tm.write_back(
            s, pid, segs[1].source_text, "ES Beta two v2.", SRC, TGT, segment=segs[1]
        )
        assert row2.id == row.id and row2.context_key == tm_common.context_key(
            "Alpha one.", "Gamma changed."
        )
        # an explicit key string wins; no context + changed target clears a stale key
        row3 = tm.write_back(
            s, pid, segs[1].source_text, "ES Beta v3.", SRC, TGT, context="ctx:" + "a" * 64
        )
        assert row3.context_key == "ctx:" + "a" * 64
        row4 = tm.write_back(s, pid, segs[1].source_text, "ES Beta v4.", SRC, TGT)
        assert row4.context_key is None
        # same target, no context: the key is kept
        row5 = tm.write_back(s, pid, segs[0].source_text, "ES Alpha one.", SRC, TGT)
        assert row5.context_key == row0.context_key
    print(
        "  7 write_back stores the context on insert and update (segment neighbours, explicit key, clear)"
    )


def test_tmx_round_trip_and_props():
    pid, _ = mkproject("rt")
    with Session(engine) as s:
        tm.write_back(
            s, pid, "Round trip one.", "Ida y vuelta uno.", SRC, TGT, context=("Prev A.", "Next A.")
        )
        tm.write_back(s, pid, "Round trip two.", "Ida y vuelta dos.", SRC, TGT)
        data = tm.export_tmx(s, pid, SRC, TGT)
    assert b"x-locos-context-key" in data
    pid2, _ = mkproject("rt2")
    with Session(engine) as s:
        tm.import_tmx(s, pid2, data, SRC, TGT)
        # the keyed unit keeps its key; the context-free one stays NULL (own export: no file-order fallback)
        r1 = s.exec(
            select(SegmentTM).where(
                SegmentTM.project_id == pid2, SegmentTM.source_text == "Round trip one."
            )
        ).one()
        r2 = s.exec(
            select(SegmentTM).where(
                SegmentTM.project_id == pid2, SegmentTM.source_text == "Round trip two."
            )
        ).one()
        assert r1.context_key == tm_common.context_key("Prev A.", "Next A."), r1.context_key
        # L4: our own export states its contexts explicitly: a unit without a key stays
        # context-free instead of inheriting its file neighbours
        assert r2.context_key is None, r2.context_key
    # Trados-style props, and the prop on the streamed (file) path
    tmx = (
        '<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="en-US" '
        'datatype="plaintext" segtype="sentence" creationtool="x" creationtoolversion="1" o-tmf="x" '
        'adminlang="en"/><body>'
        '<tu><prop type="x-context-pre">Before it.</prop><prop type="x-context-post">After it.</prop>'
        '<tuv xml:lang="en-US"><seg>Trados unit.</seg></tuv><tuv xml:lang="es-ES"><seg>Unidad Trados.</seg></tuv></tu>'
        f'<tu><prop type="x-locos-context-key">{"key:" + "b" * 64}</prop>'
        '<tuv xml:lang="en-US"><seg>Own prop unit.</seg></tuv><tuv xml:lang="es-ES"><seg>Unidad propia.</seg></tuv></tu>'
        '<tu><prop type="x-locos-context-key">not-a-key</prop>'
        '<tuv xml:lang="en-US"><seg>Bad prop unit.</seg></tuv><tuv xml:lang="es-ES"><seg>Unidad mala.</seg></tuv></tu>'
        "</body></tmx>"
    )
    path = os.path.join(_TMP, "props.tmx")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(tmx)
    pid3, _ = mkproject("rt3")
    with Session(engine) as s:
        tm.import_tmx_file(s, pid3, path, SRC, TGT)
        got = {
            r.source_text: r.context_key
            for r in s.exec(select(SegmentTM).where(SegmentTM.project_id == pid3)).all()
        }
    assert got["Trados unit."] == tm_common.context_key("Before it.", "After it."), got
    assert got["Own prop unit."] == "key:" + "b" * 64
    # a malformed prop is ignored and the file neighbours apply (previous = Own prop unit., next = none)
    assert got["Bad prop unit."] == tm_common.context_key("Own prop unit.", ""), got
    print(
        "  8 TMX round trip keeps context_key; x-context-pre/post and x-locos-context-key honoured; bad prop ignored"
    )


def _tmx(units, tool="x"):
    body = "".join(
        f"<tu>{props}"
        f'<tuv xml:lang="en-US"><seg>{s}</seg></tuv><tuv xml:lang="es-ES"><seg>{t}</seg></tuv></tu>'
        for s, t, props in units
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="en-US" '
        f'datatype="plaintext" segtype="sentence" creationtool="{tool}" creationtoolversion="1" '
        f'o-tmf="x" adminlang="en"/><body>{body}</body></tmx>'
    ).encode()


def _row(s, pid, src):
    s.expire_all()
    return s.exec(
        select(SegmentTM).where(SegmentTM.project_id == pid, SegmentTM.source_text == src)
    ).one()


def test_tmx_target_change_takes_the_unit_key():
    """H1: a TMX import that changes an existing row's target must not keep the OLD key (the
    new target would lock as a hard 101 in the old target's context)."""
    pid, _ = mkproject("h1")
    ctx = ("Before.", "After.")
    old_key = tm_common.context_key(*ctx)
    names = ("Open the lid.", "Same unit.", "Keyed change.", "Unrelated A.", "Unrelated B.")
    for streamed in (False, True):
        with Session(engine) as s:
            tm.write_back(s, pid, "Open the lid.", "Abra la tapa.", SRC, TGT, context=ctx)
            tm.write_back(s, pid, "Same unit.", "Misma unidad.", SRC, TGT, context=ctx)
            tm.write_back(s, pid, "Keyed change.", "old", SRC, TGT, context=ctx)
            s.commit()
            file_key = "key:" + ("c" if streamed else "d") * 64
            data = _tmx(
                [
                    ("Unrelated A.", "A", ""),
                    ("Open the lid.", "OTRO DESTINO", ""),
                    ("Same unit.", "Misma unidad.", ""),
                    ("Keyed change.", "X", f'<prop type="x-locos-context-key">{file_key}</prop>'),
                    ("Unrelated B.", "B", ""),
                ]
            )
            if streamed:
                path = os.path.join(_TMP, "h1.tmx")
                with open(path, "wb") as fh:
                    fh.write(data)
                tm.import_tmx_file(s, pid, path, SRC, TGT)
            else:
                tm.import_tmx(s, pid, data, SRC, TGT)
            changed = _row(s, pid, "Open the lid.")
            assert changed.target_text == "OTRO DESTINO"
            # the unit's own (file-order) key, never the old context's
            assert changed.context_key == tm_common.context_key("Unrelated A.", "Same unit.")
            assert changed.context_key != old_key
            hit = tm_hub.lookup_segments(s, pid, [("Open the lid.", old_key)], SRC, TGT)[0]
            assert hit["match_pct"] == 100, hit
            # unchanged target keeps the stored key
            assert _row(s, pid, "Same unit.").context_key == old_key
            # an explicit prop on a changed row wins
            assert _row(s, pid, "Keyed change.").context_key == file_key
            for src in names:
                s.delete(_row(s, pid, src))
            s.commit()
    # a changed target whose unit carries no context at all (lone unit) goes NULL
    with Session(engine) as s:
        tm.write_back(s, pid, "Lone change.", "viejo", SRC, TGT, context=ctx)
        s.commit()
        tm.import_tmx(s, pid, _tmx([("Lone change.", "nuevo", "")]), SRC, TGT)
        assert _row(s, pid, "Lone change.").context_key is None
    print(
        "  8b TMX import: a changed target takes the unit's key (NULL if none); unchanged keeps it"
    )


def test_tmx_repeated_new_source_takes_the_last_units_key():
    """Gate round 2 M-A: a NEW source repeated in one file with another target is stored with
    the LAST target, so it must carry the last unit's context key, not the first one's (else
    target Y would fill as a hard-locked 101 in target X's context). Run with the spool's
    flush batch at 500 (both units in one batch) and at 2 (the second unit in a later batch)."""
    pid, _ = mkproject("ma")
    units = [
        ("P1.", "p1", ""),
        ("Dup.", "X", ""),
        ("P2.", "p2", ""),
        ("Q1.", "q1", ""),
        ("Dup.", "Y", ""),
        ("Q2.", "q2", ""),
    ]
    saved = tm.TMX_IMPORT_FLUSH_BATCH
    try:
        for flush in (500, 2):
            tm.TMX_IMPORT_FLUSH_BATCH = flush
            with Session(engine) as s:
                tm.import_tmx(s, pid, _tmx(units), SRC, TGT)
                row = _row(s, pid, "Dup.")
                assert row.target_text == "Y", (flush, row.target_text)
                assert row.context_key == tm_common.context_key("Q1.", "Q2."), flush
                first_ctx = tm_common.context_key("P1.", "P2.")
                hit = tm_hub.lookup_segments(s, pid, [("Dup.", first_ctx)], SRC, TGT)[0]
                assert hit["match_pct"] == 100, (flush, hit)
                for src in ("P1.", "Dup.", "P2.", "Q1.", "Q2."):
                    s.delete(_row(s, pid, src))
                s.commit()
    finally:
        tm.TMX_IMPORT_FLUSH_BATCH = saved
    print("  8c TMX import: a repeated new source keeps the last unit's key with its target")


def test_lone_unit_tmx_has_no_file_order_context():
    """A TMX with one unit of the pair carries no evidence of document order: no ctx('', '')."""
    pid, _ = mkproject("lone")
    with Session(engine) as s:
        tm.import_tmx(s, pid, _tmx([("One only.", "Solo uno.", "")]), SRC, TGT)
        assert _row(s, pid, "One only.").context_key is None
        hit = tm_hub.lookup_segments(
            s, pid, [("One only.", tm_common.context_key("", ""))], SRC, TGT
        )[0]
        assert hit["match_pct"] == 100, hit
        # explicit props still apply to a lone unit
        props = '<prop type="x-context-pre">P.</prop><prop type="x-context-post">N.</prop>'
        tm.import_tmx(s, pid, _tmx([("Lone keyed.", "Solo con clave.", props)]), SRC, TGT)
        assert _row(s, pid, "Lone keyed.").context_key == tm_common.context_key("P.", "N.")
        # the first and last unit of a multi-unit third-party file keep the fallback
        tm.import_tmx(s, pid, _tmx([("Edge A.", "A", ""), ("Edge B.", "B", "")]), SRC, TGT)
        assert _row(s, pid, "Edge A.").context_key == tm_common.context_key("", "Edge B.")
        assert _row(s, pid, "Edge B.").context_key == tm_common.context_key("Edge A.", "")
    print(
        "  8c lone TMX unit: no ctx('','') fallback; explicit props kept; multi-unit edges keep it"
    )


def test_human_edit_keeps_the_key():
    from app.services import tm_module

    pid, _ = mkproject("edit")
    ctx = ("Before.", "After.")
    with Session(engine) as s:
        tm.write_back(s, pid, "Edit me.", "Editame.", SRC, TGT, context=ctx)
        s.commit()
        row = _row(s, pid, "Edit me.")
        tm_module.edit_entry(s, row.id, "Editame bien.")
        row = _row(s, pid, "Edit me.")
        assert row.target_text == "Editame bien." and row.context_key == tm_common.context_key(*ctx)
    print("  8d human edit of a TM target keeps the context key (a correction in the same context)")


def _load_migration():
    path = os.path.join(_ROOT, "alembic", "versions", "d6b3f8a2c4e1_segmenttm_context_key.py")
    spec = importlib.util.spec_from_file_location("mig_ctx_key", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill():
    mig = _load_migration()
    # the migration's copy of the recipes agrees with the app's
    assert mig._ctx_key("a  b", " c ") == tm_common.context_key("a b", "c")
    assert mig._key_ctx_key("ns", "k.name") == tm_common.key_context_key(
        tm_context.key_full_name("ns", "k.name")
    )
    assert mig._source_hash("  Hello   world ", SRC, TGT) == tm_common.hash_text(
        "Hello world", SRC, TGT
    )
    pid, _ = mkproject("bf")
    with Session(engine) as s:
        doc = Document(project_id=pid, filename="b.txt", fmt="txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        rows = [
            ("First.", SegStatus.NEW),
            ("Backfill me.", SegStatus.APPROVED),
            ("Third.", SegStatus.NEW),
            ("Not approved.", SegStatus.MT),
            ("Target differs.", SegStatus.APPROVED),
        ]
        for i, (t, st) in enumerate(rows):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=TGT,
                    order_index=i,
                    source_text=t,
                    target_text=f"ES {t}",
                    status=st,
                )
            )
        s.commit()
        # TM rows written WITHOUT context (as they were before the column existed)
        for t, tgt in (
            ("Backfill me.", "ES Backfill me."),
            ("Not approved.", "ES Not approved."),
            ("Target differs.", "ES something else"),
            ("No segment.", "ES No segment."),
        ):
            s.add(
                SegmentTM(
                    project_id=pid,
                    source_locale=SRC,
                    target_locale=TGT,
                    source_text=t,
                    target_text=tgt,
                    source_hash=tm_common.hash_text(t, SRC, TGT),
                )
            )
        s.commit()
        n = mig.backfill_context_keys(s.connection(), chunk=2)  # tiny chunks: the loop is exercised
        s.commit()
        got = {
            r.source_text: r.context_key
            for r in s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all()
        }
        assert n == 1, n
        assert got["Backfill me."] == tm_common.context_key("First.", "Third."), got
        assert (
            got["Not approved."] is None
            and got["Target differs."] is None
            and got["No segment."] is None
        )
        # idempotent: a second run changes nothing
        assert mig.backfill_context_keys(s.connection()) == 0
        # a key segment backfills from its key name
        sk = StringKey(project_id=pid, namespace="common", name="btn.save")
        s.add(sk)
        s.commit()
        s.refresh(sk)
        kdoc = Document(project_id=pid, filename="keys", fmt="json")
        s.add(kdoc)
        s.commit()
        s.refresh(kdoc)
        s.add(
            Segment(
                document_id=kdoc.id,
                target_locale=TGT,
                order_index=0,
                source_text="Save",
                target_text="Guardar",
                status=SegStatus.APPROVED,
                key_id=sk.id,
            )
        )
        s.add(
            SegmentTM(
                project_id=pid,
                source_locale=SRC,
                target_locale=TGT,
                source_text="Save",
                target_text="Guardar",
                source_hash=tm_common.hash_text("Save", SRC, TGT),
            )
        )
        s.commit()
        assert mig.backfill_context_keys(s.connection()) == 1
        s.commit()
        r = s.exec(
            select(SegmentTM).where(SegmentTM.project_id == pid, SegmentTM.source_text == "Save")
        ).one()
        assert r.context_key == tm_common.key_context_key(
            tm_context.key_full_name("common", "btn.save")
        )
    # an install with no segments and no TM is a no-op
    from sqlalchemy import create_engine

    empty = create_engine("sqlite://")
    from sqlmodel import SQLModel

    SQLModel.metadata.create_all(empty)
    with empty.begin() as conn:
        assert mig.backfill_context_keys(conn) == 0
    print(
        "  9 backfill: sets keys from approved segments (chunked, idempotent, key segments, no-op when empty)"
    )


def test_string_key_project_101():
    pid, _ = mkproject("keys")
    with Session(engine) as s:
        doc = Document(project_id=pid, filename="k", fmt="json")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        ka = StringKey(project_id=pid, namespace="ui", name="save")
        kb = StringKey(project_id=pid, namespace="ui", name="store")
        s.add(ka)
        s.add(kb)
        s.commit()
        s.refresh(ka)
        s.refresh(kb)
        sa_ = Segment(
            document_id=doc.id,
            target_locale=TGT,
            order_index=0,
            source_text="Save",
            target_text="Guardar",
            status=SegStatus.APPROVED,
            key_id=ka.id,
        )
        sb_ = Segment(
            document_id=doc.id,
            target_locale=TGT,
            order_index=1,
            source_text="Save",
            target_text="",
            key_id=kb.id,
        )
        s.add(sa_)
        s.add(sb_)
        s.commit()
        s.refresh(sa_)
        s.refresh(sb_)
        row = tm.write_back(s, pid, "Save", "Guardar", SRC, TGT, segment=sa_)
        assert row.context_key == tm_common.key_context_key(tm_context.key_full_name("ui", "save"))
        keys = tm_context.segment_context_keys(s, [sa_, sb_])
        hits = tm_hub.lookup_segments(s, pid, [("Save", keys[0]), ("Save", keys[1])], SRC, TGT)
        assert hits[0]["match_pct"] == 101 and hits[1]["match_pct"] == 100
        # lookup_batch takes a ready key string for a key project
        assert (
            tm_hub.lookup_batch(s, pid, ["Save"], SRC, TGT, contexts={"Save": keys[0]})["Save"][
                "match_pct"
            ]
            == 101
        )
    print(" 10 string-key project: 101 for the same key, 100 for another key with the same text")


def test_lock_plan_for_101():
    pol = tm_lock.TmPolicy(lock_ice=True, hundreds_enter_lqa=True)
    assert tm_lock.plan_hit({"match_pct": 101}, pol) == ("ice", True)
    assert tm_lock.plan_hit({"match_pct": 100}, pol) == ("100", False)
    assert (
        tm_lock.plan_hit({"match_pct": 99}, pol) is None
    )  # an old stored value never plans a fill
    off = tm_lock.TmPolicy(lock_ice=False, hundreds_enter_lqa=False)
    assert tm_lock.plan_hit({"match_pct": 101}, off) == ("100", True)
    assert tm_lock.fill_or_relock("", {"match_pct": 101, "target_text": "x"}, pol) == (
        "fill",
        "ice",
        True,
        "hard",
    )
    assert tm_lock.fill_or_relock("old", {"match_pct": 100, "target_text": "x"}, pol) is None
    for name in (
        "FUZZY_LOCK_MODES",
        "_clamp_fill_threshold",
        "variable_content_parity",
        "_var_signature",
    ):
        assert not hasattr(tm_lock, name), name
    assert not hasattr(pol, "fill_threshold") and not hasattr(pol, "context_floor")
    print(
        " 11 lock plan: 101 -> ice (hard), 100 -> soft/hard per policy, nothing below 100 plans; fuzzy helpers gone"
    )


if __name__ == "__main__":
    test_exact_recall()
    test_in_context_on_import_adjacency()
    test_latency()
    test_fuzzy_removed()
    test_near_identical_does_not_fill()
    test_101_vs_100_with_attached_source()
    test_same_source_twice_different_neighbours()
    test_write_back_stores_context_on_insert_and_update()
    test_tmx_round_trip_and_props()
    test_tmx_target_change_takes_the_unit_key()
    test_tmx_repeated_new_source_takes_the_last_units_key()
    test_lone_unit_tmx_has_no_file_order_context()
    test_human_edit_keeps_the_key()
    test_backfill()
    test_string_key_project_101()
    test_lock_plan_for_101()
    print("verify_tm_exact_only OK")
