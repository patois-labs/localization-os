"""LAW-TERM-1 write-parity golden for the terminology AUTHORITY INVERSION (T1b).

Hermetic (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider), zero network.

WHY THIS SUITE (SPEC-terminology-backend T1b, brief _build-brief-terminology-T1b.md A.5).
T1-lite made the compat ``glossaryentry``/``glossarysense`` rows the single authority
(LAW-TERM-17). T1b INVERTS that: the concept store becomes authoritative and the compat
rows become a pure PROJECTION of per-pair BINDINGS. LAW-TERM-1 (every legacy read seam is
byte-identical to ``main``) is the property that inversion must not break. This harness is
the contract that proves it: a deterministic stream of LEGACY writes (never a concept-editor
write, never a ``set_pairs`` on a populated glossary -- R6-M5) is replayed against several
synthetic corpora, the resulting compat store AND the read-seam outputs are snapshotted, and
the snapshot is compared to a GOLDEN captured ONCE from the BASE commit with the concept
editor flag OFF and NO T1b product code.

GOLDEN PROVENANCE. ``tests/glossary_write_parity_golden.json`` is captured by
``tests/capture_glossary_write_parity.py`` from the base commit named in the golden header
and the T1b build errata (alembic head ``b9e3d7c1f5a2``; flag ``terminology_concept_editor``
OFF). It was re-parented ONCE, off ``400720ce`` (main after the terminology-T2 merge) onto the
tree carrying issue #497: a re-import whose ``dnt`` cell is blank or missing now KEEPS the stored
flag, which is an intentional move of what the compat write stream emits. That re-parent is the
ONE permitted reason to regenerate, and only from a tree that already carries the reviewed change
(see ``capture_glossary_write_parity.py``). Otherwise this golden is NEVER regenerated from the
post-change tree: after the T1b inversion lands, the SAME op stream replayed on the changed tree
must reproduce it, permitting ONLY the enumerated closed-list diffs
(DIFF-1: a homonym sibling gaining a second-pair sense; DIFF-2: a target added in ``L``
binding every usable source synonym) -- both UNREACHABLE from the legacy op stream, so the
realized diff is expected EMPTY (A.5 permitted-diff list).

RUN THIS SUITE WHEN THE COMPAT WRITE PATH MOVES. It pins what ``app.services.glossary_hub.py``
writes, but it drives the write through ``glossary.import_bytes`` / ``glossary_module`` and never
names that module, so ``scripts/suite_owners.py`` does NOT report it as an owner of a
``glossary_hub`` edit. Run it by hand on any change to the glossary import path
(``.claude/rules/tests-and-pregate.md``).

SNAPSHOT NORMALIZATION (A.5). Per glossary: entries ordered by id, each carrying a
per-glossary LOCAL index (position, not the global autoincrement id -- so a global id offset
from an unrelated corpus never masquerades as a divergence, while LAW-TERM-3 id STABILITY is
asserted separately by the round-trip test), ``source_locale``, ``source_term``,
``target_locale``, ``target_term``, ``dnt``, ``case_sensitive``, ``meta`` as a PARSED dict
(``None`` == ``{}`` == absent); each with its ordered senses carrying a per-glossary concept
RANK (so "which senses share a sibling concept" is captured relationally without a raw id),
``sense_label``, ``context_cue``, ``target_term``. PLUS the read-seam outputs per configured
locale pair: ``match_terms``, ``prompts.glossary_line``, ``match_terms_merged`` (pinned and
legacy-inclusive plan shapes), ``quality.dnt_violations``,
``quality.glossary_term_violations``, and the ``_choose_sense`` option-list prompt string.

The PROJECTION-EQUIVALENCE assertion (``project(backfill(strip(X))) == X``) and the
LAW-TERM-3 import->export->import id-stability round trip both exercise T1b-only functions
(``glossary_concepts.project`` / ``backfill_pair_bindings``, ungated ``from_v1``); they are
GUARDED behind ``hasattr(glossary_concepts, "project")`` so this file is GREEN on the base
tree (COMMIT 1) and fully active after the inversion lands.

Final line: "GLOSSARY WRITE PARITY OK".
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_writeparity_")

import random

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    GlossaryEntry,
    GlossaryPair,
    GlossarySense,
)
from app.services import (
    glossary,
    glossary_concepts,
    glossary_module,
    prompts,
    quality,
)
from app.services.glossary_module_locale_canonicalization_ import GlossaryPlan
from app.services.providers import MockProvider

init_db()

_GOLDEN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "glossary_write_parity_golden.json"
)
_HAS_PROJECT = hasattr(glossary_concepts, "project")

# The five-key compat projection every read seam agrees on (LAW-TERM-1/E5).
_FIVE = ("source_term", "target_term", "sense_label", "dnt", "case_sensitive")
_META_KEYS = ("part_of_speech", "definition", "usage_example", "subject_field", "notes")


class _Canned(MockProvider):
    """Spend-exempt, deterministic, network-free sense selector: always chooses option 0
    (the base target), so ``_choose_sense`` returns None and ``_emit_for_entry`` falls back
    to the entry's base target on EVERY multi-sense term. ``_choose_sense`` is frozen by T1b
    (a read seam), so the same store yields the same read-seam output on both trees."""

    def __init__(self):
        self.prompts: list[str] = []

    def complete(self, prompt, system=None, task=None, payload=None, **kw) -> str:
        self.prompts.append(prompt)
        return '{"index": 0}'


_PROV = _Canned()


# --------------------------------------------------------------- corpus builders ---
_N = 0


def _new_glossary(pairs):
    global _N
    _N += 1
    with Session(engine) as s:
        g = glossary_module.create(s, name=f"WriteParity {_N}")
        if pairs:
            glossary_module.set_pairs(s, g.id, list(pairs))
        s.commit()
        return g.id


def _entries(gid):
    with Session(engine) as s:
        return list(
            s.exec(
                select(GlossaryEntry.id)
                .where(GlossaryEntry.glossary_id == gid)
                .order_by(GlossaryEntry.id)
            ).all()
        )


# op-stream vocabulary -- small + fixed so the stream is deterministic and reproducible.
_SRC = ["invoice", "Invoice", "bank", "Bank", "cloud", "sort", "Acme", "widget", "gift", "color"]
_TGT = ["factura", "recibo", "banco", "orilla", "nube", "ordenar", "regalo", "colour", ""]
_META_VALS = {
    "part_of_speech": "noun",
    "definition": "a bill for goods",
    "usage_example": "Send the invoice",
    "subject_field": "finance",
    "notes": "keep 30 days",
}


def _partial_meta(rng):
    keys = [k for k in _META_KEYS if rng.random() < 0.4]
    return {k: _META_VALS[k] for k in keys} or None


def _fuzz(gid, sl, tl, seed, n_ops):
    """Replay ``n_ops`` deterministic LEGACY writes against glossary ``gid``. Only the legacy
    adapters (never the concept editor, never set_pairs on a populated glossary -- R6-M5)."""
    rng = random.Random(seed)
    # locales an op may use: the pair's own, plus the wildcard "" (a documented any-locale term).
    src_locs = [sl, sl, ""]
    tgt_locs = [tl, tl, ""]
    for _ in range(n_ops):
        op = rng.random()
        cur = _entries(gid)
        with Session(engine) as s:
            if op < 0.40 or not cur:
                glossary_module.add_term(
                    s,
                    gid,
                    source_term=rng.choice(_SRC),
                    target_term=rng.choice(_TGT),
                    source_locale=rng.choice(src_locs),
                    target_locale=rng.choice(tgt_locs),
                    dnt=rng.random() < 0.25,
                    meta=_partial_meta(rng),
                )
            elif op < 0.62:
                eid = rng.choice(cur)
                kind = rng.random()
                if kind < 0.35:  # target change
                    glossary_module.edit_term(s, gid, eid, target_term=rng.choice(_TGT))
                elif kind < 0.55:  # target BLANK
                    glossary_module.edit_term(s, gid, eid, target_term="")
                elif kind < 0.80:  # dnt + meta REPLACE
                    glossary_module.edit_term(
                        s, gid, eid, dnt=rng.random() < 0.5, meta=_partial_meta(rng) or {}
                    )
                else:  # meta CLEAR
                    glossary_module.edit_term(s, gid, eid, meta={})
            elif op < 0.78:
                eid = rng.choice(cur)
                glossary_module.add_sense(
                    s,
                    gid,
                    eid,
                    sense_label=rng.choice(["finance", "geography", "tech"]),
                    target_term=rng.choice(_TGT),  # incl. "" -> blank-target sense
                    context_cue=rng.choice(["", "money context", "map context"]),
                )
            elif op < 0.90:
                glossary_module.remove_term(s, gid, rng.choice(cur))
            else:  # multi-row CSV import (sense columns + present-but-blank meta cells)
                rows = [
                    b"source_term,target_term,dnt,sense_label,context_cue,definition,"
                    b"subject_field,part_of_speech,usage_example,notes"
                ]
                for _r in range(rng.randint(1, 4)):
                    st = rng.choice(_SRC)
                    tt = rng.choice(_TGT)
                    dnt = "1" if rng.random() < 0.2 else ""
                    label = rng.choice(["", "", "finance"])
                    dfn = rng.choice(["", "a definition"])
                    rows.append(f"{st},{tt},{dnt},{label},,{dfn},,,,".encode())
                data = b"\r\n".join(rows) + b"\r\n"
                glossary.import_bytes(
                    s, None, data, sl or "en-US", tl or "es-ES", glossary_id=gid, filename="f.csv"
                )


# fixed pair configs cycled across the fuzz seeds (standard / two-target / reversed / self /
# other-region / pairless), so the stream covers every locale shape A.5 enumerates. Blank
# (wildcard) locales are exercised by the fuzz's ``""`` term locales, not by a blank PAIR
# (set_pairs refuses a pair with a blank side); the pairless config covers the no-pair path.
_PAIR_CONFIGS = [
    [("en-US", "es-ES")],
    [("en-US", "es-ES"), ("en-US", "fr-FR")],
    [("es-ES", "en-US")],
    [("en-US", "en-US")],
    [("en-US", "de-DE")],
    [],
]


def _build_fuzz_corpora():
    out = {}
    for i in range(10):  # >=10 seeds
        pairs = _PAIR_CONFIGS[i % len(_PAIR_CONFIGS)]
        sl, tl = pairs[0] if pairs else ("en-US", "es-ES")
        gid = _new_glossary(pairs)
        _fuzz(gid, sl, tl, seed=1000 + i, n_ops=120)  # >=120 ops
        out[f"fuzz{i}"] = gid
    return out


def _build_shape_corpora():
    """The specific shapes A.5 names, each deterministic."""
    out = {}

    # orphan,, empty-target row + wildcard + case variants + two source synonyms in one pair.
    gid = _new_glossary([("en-US", "es-ES")])
    with Session(engine) as s:
        glossary_module.add_term(s, gid, "orphan", "", "en-US", "es-ES")  # orphan,,
        glossary_module.add_term(s, gid, "invoice", "factura", "en-US", "es-ES")
        glossary_module.add_term(s, gid, "bill", "factura", "en-US", "es-ES")  # 2nd synonym
        glossary_module.add_term(s, gid, "Acme", "", "", "", dnt=True)  # wildcard DNT
        glossary_module.add_term(s, gid, "iPhone", "iPhone", "en-US", "es-ES")
        glossary_module.edit_term(
            s, gid, _entries(gid)[-1], target_term="iphone"
        )  # LT2-2 case-only surface change
    out["shapes"] = gid

    # reversed + self pairs (a rival target surface across two concepts).
    gid = _new_glossary([("es-ES", "en-US"), ("en-US", "en-US")])
    with Session(engine) as s:
        glossary_module.add_term(s, gid, "factura", "invoice", "es-ES", "en-US")
        glossary_module.add_term(s, gid, "Widget", "Widget", "en-US", "en-US")  # self
    out["reversed_self"] = gid

    # a two-target status ladder via CSV (invoice -> two targets on one concept).
    gid = _new_glossary([("en-US", "es-ES")])
    with Session(engine) as s:
        data = b"source_term,target_term,dnt\r\ninvoice,factura\r\ninvoice,recibo\r\n"
        glossary.import_bytes(s, None, data, "en-US", "es-ES", glossary_id=gid, filename="l.csv")
    out["ladder"] = gid

    # a blank-target sense by BOTH routes, then an add_term (the sense survives).
    gid = _new_glossary([("en-US", "es-ES")])
    with Session(engine) as s:
        glossary_module.add_term(s, gid, "sort", "ordenar", "en-US", "es-ES")
        eid = _entries(gid)[0]
        glossary_module.add_sense(s, gid, eid, "finance", "", "money")  # add_sense blank target
    with Session(engine) as s:
        data = b"source_term,target_term,dnt,sense_label,context_cue\r\nsort,ordenar,,tech,\r\n"
        glossary.import_bytes(s, None, data, "en-US", "es-ES", glossary_id=gid, filename="s.csv")
    with Session(engine) as s:
        glossary_module.add_term(s, gid, "sort", "clasificar", "en-US", "es-ES")
    out["blank_sense"] = gid

    return out


# ----------------------------------------------------------------------- snapshot ---
def _meta(v):
    return dict(v) if v else None


def _snapshot(gid):
    with Session(engine) as s:
        entries = list(
            s.exec(
                select(GlossaryEntry)
                .where(GlossaryEntry.glossary_id == gid)
                .order_by(GlossaryEntry.id)
            ).all()
        )
        eids = [e.id for e in entries]
        senses_by_entry: dict[int, list] = {}
        if eids:
            for sn in s.exec(
                select(GlossarySense)
                .where(GlossarySense.glossary_entry_id.in_(eids))
                .order_by(GlossarySense.id)
            ).all():
                senses_by_entry.setdefault(sn.glossary_entry_id, []).append(sn)
        # per-glossary concept RANK for a sense's sibling concept (first-appearance order).
        concept_rank: dict[int, int] = {}

        def _rank(cid):
            if cid is None:
                return None
            if cid not in concept_rank:
                concept_rank[cid] = len(concept_rank)
            return concept_rank[cid]

        rows = []
        for idx, e in enumerate(entries):
            senses = [
                [_rank(sn.concept_id), sn.sense_label, sn.context_cue, sn.target_term]
                for sn in senses_by_entry.get(e.id, [])
            ]
            rows.append(
                {
                    "i": idx,
                    "sl": e.source_locale,
                    "st": e.source_term,
                    "tl": e.target_locale,
                    "tt": e.target_term,
                    "dnt": bool(e.dnt),
                    "cs": bool(e.case_sensitive),
                    "meta": _meta(e.meta),
                    "senses": senses,
                }
            )
        pairs = [
            (p.source_locale, p.target_locale)
            for p in s.exec(select(GlossaryPair).where(GlossaryPair.glossary_id == gid)).all()
        ]
        if not pairs:  # pairless glossary: probe the distinct pairs present on its entries.
            pairs = sorted({(e.source_locale or "", e.target_locale or "") for e in entries})
        seg = " ".join(sorted({e.source_term for e in entries})) + " lorem ipsum text"
        seams = {}
        for sl, tl in pairs:
            terms = glossary.match_terms(s, None, seg, sl, tl, glossary_id=gid, provider=_PROV)
            plan_pinned = GlossaryPlan(glossary_ids=(gid,), include_legacy=False)
            plan_legacy = GlossaryPlan(glossary_ids=(gid,), include_legacy=True)
            merged_p = glossary_module.match_terms_merged(
                s, None, seg, sl, tl, plan_pinned, provider=_PROV
            )
            merged_l = glossary_module.match_terms_merged(
                s, None, seg, sl, tl, plan_legacy, provider=_PROV
            )
            seams[f"{sl}>{tl}"] = {
                "match_terms": [[t[k] for k in _FIVE] for t in terms],
                "glossary_line": prompts.glossary_line(terms),
                "merged_pinned": [[t[k] for k in _FIVE] for t in merged_p],
                "merged_legacy": [[t[k] for k in _FIVE] for t in merged_l],
                "dnt_violations": quality.dnt_violations(seg, "", terms),
                "term_violations": quality.glossary_term_violations(
                    seg, "", terms, target_locale=tl
                ),
            }
        # the _choose_sense option-list prompt string for every entry that carries senses.
        choose = []
        for e in entries:
            sens = senses_by_entry.get(e.id, [])
            if not sens:
                continue
            prov = _Canned()
            glossary._choose_sense("probe segment", sens, prov, base_target=e.target_term)
            choose.append(prov.prompts[-1] if prov.prompts else "")
        return {"entries": rows, "seams": seams, "choose_sense": choose}


def capture_all():
    """Build every corpus in a FIXED order and return the full snapshot dict. Used by both
    the capture script and the golden comparison, so the two can never drift."""
    snap = {}
    for name, gid in {**_build_fuzz_corpora(), **_build_shape_corpora()}.items():
        snap[name] = _snapshot(gid)
    return snap


# --------------------------------------------------------------- golden comparison ---
def _load_golden():
    with open(_GOLDEN, encoding="utf-8") as f:
        doc = json.load(f)
    # the golden carries a provenance header (SHA/flag/alembic head); the corpora live under
    # "corpora" (A.5: "the golden file's own header").
    return doc.get("corpora", doc)


def _norm(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def test_write_parity_golden():
    got = capture_all()
    golden = _load_golden()
    assert set(got) == set(golden), f"corpus set drift: got {sorted(got)} golden {sorted(golden)}"
    diffs = []
    for name in golden:
        if _norm(got[name]) != _norm(golden[name]):
            diffs.append(name)
    # DIFF-1 / DIFF-2 (A.5 permitted-diff list) are UNREACHABLE from the legacy op stream, so
    # the realized diff is expected EMPTY on both the base tree (self-check) and post-T1b.
    assert not diffs, (
        "write-parity divergence in corpora "
        + ", ".join(diffs)
        + " -- not a DIFF-1/DIFF-2 shape (see A.5 permitted-diff list / the T1b errata). "
        "First divergence detail:\n" + _first_diff(got, golden, diffs[0])
    )
    print(f"  write-parity golden: {len(golden)} corpora byte-identical OK")


def _first_diff(got, golden, name):
    g, o = got[name], golden[name]
    # entries
    ge, oe = g["entries"], o["entries"]
    if len(ge) != len(oe):
        return f"  {name}: entry count {len(ge)} != golden {len(oe)}"
    for a, b in zip(ge, oe, strict=False):
        if _norm(a) != _norm(b):
            return f"  {name}: entry {a.get('i')} {_norm(a)}\n    golden {_norm(b)}"
    if _norm(g["seams"]) != _norm(o["seams"]):
        return f"  {name}: read-seam divergence"
    if _norm(g["choose_sense"]) != _norm(o["choose_sense"]):
        return f"  {name}: _choose_sense option-list divergence"
    return f"  {name}: divergence"


# ------------------------------------ projection equivalence (T1b-only, guarded) ---
def _store_derived_snapshot(gid):
    """Derived columns only, keyed by (source_locale, source_term, target_locale) so it is
    stable across a strip->backfill->project round trip that keeps ids (LAW-TERM-3) but is
    compared by content. Used by the projection-equivalence assertion."""
    with Session(engine) as s:
        entries = list(s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all())
        eids = [e.id for e in entries]
        senses = (
            list(
                s.exec(select(GlossarySense).where(GlossarySense.glossary_entry_id.in_(eids))).all()
            )
            if eids
            else []
        )
        sens_by_entry: dict[int, list] = {}
        for sn in senses:
            sens_by_entry.setdefault(sn.glossary_entry_id, []).append(
                (sn.sense_label, sn.context_cue, sn.target_term)
            )
        out = {}
        for e in entries:
            out[(e.source_locale, e.source_term, e.target_locale, e.term_id)] = (
                e.target_term,
                bool(e.dnt),
                bool(e.case_sensitive),
                _norm(_meta(e.meta)),
                sorted(sens_by_entry.get(e.id, [])),
            )
        return out


def test_projection_equivalence():
    """A.3/R6-H2: ``project(backfill(strip(X))) == X`` for the derived columns, on every
    corpus. T1b-only (needs ``project``/``backfill_pair_bindings``); a no-op on the base tree
    where the golden self-check above is the whole story."""
    if not _HAS_PROJECT:
        print("  projection-equivalence: SKIP (base tree, no project()/backfill_pair_bindings)")
        return
    from sqlalchemy import update as sa_update

    checked = 0
    for _name, gid in {**_build_fuzz_corpora(), **_build_shape_corpora()}.items():
        before = _store_derived_snapshot(gid)
        with Session(engine) as s:
            # STRIP first (A.5): NULL meta_overrides + target_term_id on every row of the
            # glossary, else the NULL-guarded backfill is a no-op comparing a store to itself.
            s.execute(
                sa_update(GlossaryEntry)
                .where(GlossaryEntry.glossary_id == gid)
                .values(meta_overrides=None, target_term_id=None)
            )
            s.commit()
            glossary_concepts.backfill_pair_bindings(s, gid)
            glossary_concepts.project(s, gid)
            s.commit()
        after = _store_derived_snapshot(gid)
        assert after == before, f"projection-equivalence broke for {_name}"
        checked += 1
    print(f"  projection-equivalence: {checked} corpora project(backfill(strip(X)))==X OK")


def test_id_stability_roundtrip():
    """LAW-TERM-3: import -> export -> import keeps ``GlossaryEntry.id`` stable. Uses the
    ungated ``from_v1`` T1b ships; guarded to the T1b tree (base ``from_v1`` refuses a
    populated import with the flag OFF)."""
    if not _HAS_PROJECT:
        print("  id-stability round trip: SKIP (base tree)")
        return
    gid = _build_shape_corpora()["shapes"]
    with Session(engine) as s:
        before = {
            (e.source_locale, e.source_term, e.target_locale): e.id
            for e in s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
        }
        doc = glossary_concepts.to_v1(s, gid)
    with Session(engine) as s:
        glossary_concepts.from_v1(s, gid, doc)
    with Session(engine) as s:
        after = {
            (e.source_locale, e.source_term, e.target_locale): e.id
            for e in s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
        }
    for k, v in before.items():
        assert after.get(k) == v, f"entry id for {k} changed {v} -> {after.get(k)}"
    print(f"  id-stability round trip: {len(before)} entry ids stable across round-trip OK")


def main():
    test_write_parity_golden()
    test_projection_equivalence()
    test_id_stability_roundtrip()
    print("GLOSSARY WRITE PARITY OK")


if __name__ == "__main__":
    main()
