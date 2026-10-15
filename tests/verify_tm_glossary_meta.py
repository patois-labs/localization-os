"""TM + glossary META provenance verification (SPEC-context-metadata phase 4) — hermetic.

Throwaway LOC_TMS_DATA_DIR + blanked provider keys AND LOCAL_BASE_URL (no live LM
Studio). ZERO network. The mock engine is deterministic, so every assertion is exact.

Phase 4 (this builder's slice): TM writes stamp SegmentTM.meta provenance; TMX export
carries it as <prop type="x-..."> lines + standard provenance attrs and TMX import reads
them back (round-trip); glossary CSV import/export gains the four TBX-ish meta columns.

The load-bearing invariant: meta is ADDITIVE provenance, NEVER a match key. A row with no
meta exports BYTE-IDENTICAL to before, and TM matching (lookup + example_matches_batch) is
unaffected by whether meta is present.

Covers:
  write_back stamp   — origin="human_approved" (+ content_type name when typed).
  tmx export props   — x-origin/x-content_type/... + creationtool/creationdate/usagecount.
  tmx round-trip     — export -> import preserves meta; foreign/absent props tolerated.
  byte-stable        — a meta-less TM exports identical to a hand-built plain TMX.
  match-unaffected   — lookup + example_matches_batch identical with/without meta.
  glossary csv       — export has the 4 TBX cols; import reads them to meta; a legacy
                       CSV (no cols) imports unchanged (back-compat) + round-trips.

Final line: "TM GLOSSARY META OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tgmeta_")

from lxml import etree
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import GlossaryEntry, Project, SegmentTM
from app.services import content_types as ctsvc
from app.services import glossary, tm
from app.services.tm import XML_LANG

SRC, TGT = "en-US", "fr-FR"


def _project(s, name, content_type_id=None):
    p = Project(name=name, source_locale=SRC, content_type_id=content_type_id)
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def _plain_tmx(pairs, src, tgt) -> bytes:
    """Rebuild export_tmx's EXACT serialization for meta-less rows (the pre-phase-4
    output) so the byte-stability assertion is a real byte-for-byte comparison."""
    tmx = etree.Element("tmx", version="1.4")
    header = etree.SubElement(tmx, "header")
    header.set("creationtool", "Localization OS")
    header.set("segtype", "sentence")
    header.set("srclang", src)
    header.set("datatype", "plaintext")
    body = etree.SubElement(tmx, "body")
    for s_text, t_text in pairs:
        tu = etree.SubElement(body, "tu")
        for lang, text in ((src, s_text), (tgt, t_text)):
            tuv = etree.SubElement(tu, "tuv")
            tuv.set(XML_LANG, lang)
            seg = etree.SubElement(tuv, "seg")
            seg.text = text
    return etree.tostring(tmx, xml_declaration=True, encoding="utf-8", pretty_print=True)


# ===========================================================================
def test_write_back_stamps_provenance():
    """write_back stamps meta.origin='human_approved'; content_type name is added when
    the project is typed, omitted when it is untyped."""
    with Session(engine) as s:
        X = ctsvc.create(s, "Help article").id
        p = _project(s, "Typed", content_type_id=X)
        r = tm.write_back(s, p.id, "Adventure awaits.", "L'aventure attend.", SRC, TGT)
        assert r.meta == {"origin": "human_approved", "content_type": "Help article"}, r.meta

        pu = _project(s, "Untyped")
        ru = tm.write_back(s, pu.id, "Neutral phrase.", "Phrase neutre.", SRC, TGT)
        assert ru.meta == {"origin": "human_approved"}, ru.meta

        # re-approving the SAME source re-stamps current provenance (still human).
        r2 = tm.write_back(s, p.id, "Adventure awaits.", "Nouvelle cible.", SRC, TGT)
        assert r2.id == r.id and r2.target_text == "Nouvelle cible."
        assert r2.meta == {"origin": "human_approved", "content_type": "Help article"}, r2.meta
    print("  write_back: origin=human_approved (+content_type when typed): OK")


def test_tmx_export_emits_props_and_attrs():
    """A meta'd row exports its x-* props + creationtool/creationdate/usagecount."""
    with Session(engine) as s:
        p = _project(s, "ExportProps")
        row = SegmentTM(
            project_id=p.id,
            source_locale=SRC,
            target_locale=TGT,
            source_text="Reset your password.",
            target_text="Reinitialisez.",
            source_hash=tm.hash_text("Reset your password.", SRC, TGT),
            use_count=7,
            meta={
                "origin": "human_approved",
                "content_type": "help article",
                "domain": "software UI",
                "context_prev": "Open settings.",
                "context_next": "Confirm the change.",
            },
        )
        s.add(row)
        s.commit()
        xml = tm.export_tmx(s, p.id, SRC, TGT)
    root = etree.fromstring(xml)
    tu = root.find("body/tu")
    assert tu.get("creationtool") == "Localization OS", xml
    assert tu.get("usagecount") == "7", xml
    assert tu.get("creationdate") and tu.get("creationdate").endswith("Z"), xml
    props = {pr.get("type"): "".join(pr.itertext()) for pr in tu.findall("prop")}
    assert props == {
        "x-origin": "human_approved",
        "x-content_type": "help article",
        "x-domain": "software UI",
        "x-context_prev": "Open settings.",
        "x-context_next": "Confirm the change.",
    }, props
    # props must precede tuv (TMX DTD) — first child is a prop, tuvs come after.
    kinds = [child.tag for child in tu]
    assert kinds[0] == "prop" and kinds[-1] == "tuv", kinds
    print("  tmx export: x-* props + creationtool/creationdate/usagecount, prop-before-tuv: OK")


def test_tmx_roundtrip_preserves_meta():
    """export -> import preserves a row's meta; unknown props + absent props tolerated."""
    with Session(engine) as s:
        p1 = _project(s, "RTsrc")
        s.add(
            SegmentTM(
                project_id=p1.id,
                source_locale=SRC,
                target_locale=TGT,
                source_text="Save changes.",
                target_text="Enregistrer.",
                source_hash=tm.hash_text("Save changes.", SRC, TGT),
                meta={"origin": "connector:zendesk", "content_type": "support"},
            )
        )
        s.commit()
        xml = tm.export_tmx(s, p1.id, SRC, TGT)

        p2 = _project(s, "RTdst")
        res = tm.import_tmx(s, p2.id, xml, SRC, TGT)
        assert res["imported"] == 1, res
        imported = s.exec(select(SegmentTM).where(SegmentTM.project_id == p2.id)).first()
        assert imported.meta == {"origin": "connector:zendesk", "content_type": "support"}, (
            imported.meta
        )

        # foreign prop + a recognized one: unknown ignored, known kept.
        foreign = (
            """<?xml version="1.0"?><tmx version="1.4"><body>"""
            """<tu><prop type="x-unknown">ignore me</prop>"""
            """<prop type="x-origin">mt</prop>"""
            f"""<tuv xml:lang="{SRC}"><seg>Foreign unit.</seg></tuv>"""
            f"""<tuv xml:lang="{TGT}"><seg>Unite.</seg></tuv></tu>"""
            """</body></tmx>"""
        ).encode()
        p3 = _project(s, "RTforeign")
        tm.import_tmx(s, p3.id, foreign, SRC, TGT)
        fr = s.exec(select(SegmentTM).where(SegmentTM.project_id == p3.id)).first()
        assert fr.meta == {"origin": "mt"}, fr.meta

        # legacy propless TMX -> meta stays None (byte-stable, back-compat).
        legacy = (
            """<?xml version="1.0"?><tmx version="1.4"><body>"""
            f"""<tu><tuv xml:lang="{SRC}"><seg>Legacy unit.</seg></tuv>"""
            f"""<tuv xml:lang="{TGT}"><seg>Unite legacy.</seg></tuv></tu>"""
            """</body></tmx>"""
        ).encode()
        p4 = _project(s, "RTlegacy")
        tm.import_tmx(s, p4.id, legacy, SRC, TGT)
        lr = s.exec(select(SegmentTM).where(SegmentTM.project_id == p4.id)).first()
        assert lr.meta is None, lr.meta
    print("  tmx round-trip: meta preserved; unknown props ignored; propless -> None: OK")


def test_no_meta_row_exports_byte_identical():
    """A TM whose rows all have meta=None exports byte-for-byte the pre-phase-4 output."""
    with Session(engine) as s:
        p = _project(s, "ByteStable")
        pairs = [("Hello world.", "Bonjour le monde."), ("Goodbye.", "Au revoir.")]
        for src_t, tgt_t in pairs:
            s.add(
                SegmentTM(
                    project_id=p.id,
                    source_locale=SRC,
                    target_locale=TGT,
                    source_text=src_t,
                    target_text=tgt_t,
                    source_hash=tm.hash_text(src_t, SRC, TGT),
                    meta=None,
                )
            )
        s.commit()
        xml = tm.export_tmx(s, p.id, SRC, TGT)
    assert xml == _plain_tmx(pairs, SRC, TGT), xml
    assert b"<prop" not in xml and b"creationdate" not in xml and b"usagecount" not in xml
    print("  byte-stable: meta-less TM export == hand-built plain TMX (no props/attrs): OK")


def test_matching_unaffected_by_meta():
    """lookup() returns the SAME match whether or not the matched row carries meta —
    meta is never a match input."""

    def _seed(s, pid, meta):
        rows = [
            ("The quick brown fox jumps.", "Le renard brun rapide saute."),
            ("An unrelated sentence entirely.", "Une phrase sans rapport."),
        ]
        for src_t, tgt_t in rows:
            s.add(
                SegmentTM(
                    project_id=pid,
                    source_locale=SRC,
                    target_locale=TGT,
                    source_text=src_t,
                    target_text=tgt_t,
                    source_hash=tm.hash_text(src_t, SRC, TGT),
                    meta=meta,
                )
            )
        s.commit()

    with Session(engine) as s:
        pa = _project(s, "MatchNoMeta")
        pb = _project(s, "MatchMeta")
        _seed(s, pa.id, None)
        _seed(
            s,
            pb.id,
            {
                "origin": "human_approved",
                "content_type": "marketing",
                "domain": "x",
                "context_prev": "y",
                "context_next": "z",
            },
        )

        # exact-hash match
        q_exact = "The quick brown fox jumps."
        ea = tm.lookup(s, pa.id, q_exact, SRC, TGT)
        eb = tm.lookup(s, pb.id, q_exact, SRC, TGT)
        assert ea and eb
        assert ea["match_pct"] == eb["match_pct"] == 100
        assert ea["target_text"] == eb["target_text"], (ea, eb)

        # fuzzy match (a near variant)
        q_fuzzy = "The quick brown fox jumped."
        fa = tm.lookup(s, pa.id, q_fuzzy, SRC, TGT)
        fb = tm.lookup(s, pb.id, q_fuzzy, SRC, TGT)
        assert fa and fb, (fa, fb)
        assert fa["match_pct"] == fb["match_pct"], (fa, fb)
        assert fa["target_text"] == fb["target_text"], (fa, fb)

        # example_matches_batch (TM few-shot, phase 2B) — identical with/without meta.
        exa = tm.example_matches_batch(s, pa.id, [q_fuzzy], SRC, TGT)
        exb = tm.example_matches_batch(s, pb.id, [q_fuzzy], SRC, TGT)
        assert exa[q_fuzzy] == exb[q_fuzzy], (exa, exb)
        assert exa[q_fuzzy], "expected at least one few-shot example over the floor"
    print("  match-unaffected: lookup (exact+fuzzy) + example_matches_batch identical: OK")


def test_glossary_csv_meta_columns():
    """Import reads the 4 TBX-ish columns into GlossaryEntry.meta; export emits them;
    round-trip preserves them; a legacy CSV (no columns) imports unchanged + stable."""
    with Session(engine) as s:
        p = _project(s, "GlossMeta")
        csv_in = (
            b"source_term,target_term,dnt,part_of_speech,definition,usage_example,subject_field\r\n"
            b"widget,bidule,,noun,A small UI control,Click the widget.,software UI\r\n"
            b"API,API,true,,,,software UI\r\n"
        )
        res = glossary.import_csv(s, p.id, csv_in, SRC, TGT)
        assert res["entries"] == 2, res
        widget = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.project_id == p.id, GlossaryEntry.source_term == "widget"
            )
        ).first()
        assert widget.meta == {
            "part_of_speech": "noun",
            "definition": "A small UI control",
            "usage_example": "Click the widget.",
            "subject_field": "software UI",
        }, widget.meta
        api = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.project_id == p.id, GlossaryEntry.source_term == "API"
            )
        ).first()
        assert api.meta == {"subject_field": "software UI"}, api.meta
        assert api.dnt is True

        # export: header carries all 4 TBX cols; widget row carries its values.
        out = glossary.export_csv(s, p.id).decode("utf-8")
        header = out.splitlines()[0]
        for col in ("part_of_speech", "definition", "usage_example", "subject_field"):
            assert col in header, header
        assert "A small UI control" in out and "Click the widget." in out, out

        # round-trip: import the export into a fresh project -> same meta.
        p2 = _project(s, "GlossMeta2")
        glossary.import_csv(s, p2.id, out.encode("utf-8"), SRC, TGT)
        w2 = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.project_id == p2.id, GlossaryEntry.source_term == "widget"
            )
        ).first()
        assert w2.meta == widget.meta, (w2.meta, widget.meta)

        # legacy CSV WITHOUT the meta columns: imports unchanged (meta stays None).
        p3 = _project(s, "GlossLegacy")
        legacy = b"source_term,target_term,dnt\r\nButton,Bouton,\r\n"
        glossary.import_csv(s, p3.id, legacy, SRC, TGT)
        btn = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.project_id == p3.id, GlossaryEntry.source_term == "Button"
            )
        ).first()
        assert btn.meta is None, btn.meta
        assert btn.target_term == "Bouton"
        # its export has the 4 cols in the header but blank cells for this term.
        leg_out = glossary.export_csv(s, p3.id).decode("utf-8")
        assert "part_of_speech" in leg_out.splitlines()[0]
        assert leg_out.rstrip().endswith("Button,Bouton,,,,,,") or "Button,Bouton," in leg_out
    print("  glossary CSV: 4 TBX cols import->meta + export + round-trip; legacy unchanged: OK")


def main():
    init_db()
    test_write_back_stamps_provenance()
    test_tmx_export_emits_props_and_attrs()
    test_tmx_roundtrip_preserves_meta()
    test_no_meta_row_exports_byte_identical()
    test_matching_unaffected_by_meta()
    test_glossary_csv_meta_columns()
    print("TM GLOSSARY META OK")


if __name__ == "__main__":
    main()
