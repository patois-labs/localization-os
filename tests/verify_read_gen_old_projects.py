"""Old projects keep the pre-11-07 reading (#810, #814.1 gate, end to end).

A document imported before the 11-07 reading rules (orphan HTML end tags as codes, rarer declared
charsets, real codes numbered around token look-alike text) has no `read_gen` stamp. Through the
real services it must repair (re-ingest) to the same units and export to the same bytes as the
code before the fix did (the expected values below were produced by build/issues-1107 at
efaa46c64, before the fix). Families: HTML (orphan end tag, ISO-8859-8, look-alike), XLIFF, MDX,
WebVTT, SubRip, DOCX. Each family with a version check is then exported once more with that check
switched off, and must come out different (the check is what keeps the old projects stable).

An old import is made by importing under the old rules (base._LEGACY_READ), which is exactly what
the code did before the fix: the new rules only run where legacy_read() is False.

Hermetic and offline: throwaway LOC_TMS_DATA_DIR, mock provider. ASCII output only.
"""

import contextlib
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import bilingua_switch_off, init_env

init_env("loctms_readgen_old_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.filters import base
from app.models import Document, Project, Segment, TargetLocale

init_db()

X, Y = "⟦", "⟧"
HEB = "שלום"


def tok(s: str) -> str:
    return X + s + Y


def _docx() -> bytes:
    import docx

    d = docx.Document()
    p = d.add_paragraph()
    p.add_run("Use ")
    p.add_run("bold").bold = True
    p.add_run(" now.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


XLF = (
    '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
    '<file source-language="en" target-language="fr" datatype="plaintext" original="a">'
    '<body><trans-unit id="1"><source>Use ' + tok("x1") + ' and <g id="1">bold</g> now'
    "</source></trans-unit></body></file></xliff>"
).encode()

# (filename, upload, source locale, units, export, versioned)
CASES = [
    (
        "orphan.html",
        b"<p>Hello</b> big <i>world</i></p>",
        "en-US",
        [f"Hello big {tok('g1')}world{tok('/g1')}"],
        b"<p>X Hello big <i>world</i></p>",
        True,
    ),
    (
        "heb.html",
        f'<meta charset="iso-8859-8"><p>{HEB}</p>'.encode("iso-8859-8"),
        "he-IL",
        [HEB],
        b'<meta charset="UTF-8"><p>X ' + HEB.encode() + b"</p>",
        False,  # he-IL: the source-language code page (Windows-1255) reads it either way
    ),
    (
        "heb-en.html",
        f'<meta charset="iso-8859-8"><p>{HEB}</p>'.encode("iso-8859-8"),
        "en-US",
        [HEB.encode("iso-8859-8").decode("cp1252")],
        None,  # only the units are pinned: the export is the same mojibake text
        True,
    ),
    (
        "look.html",
        f"<p>Use {tok('g1')} and <b>bold</b> here</p>".encode(),
        "en-US",
        [f"Use {tok('g1')} and {tok('g1')}bold{tok('/g1')} here"],
        b"<p>X Use <b> and <b>bold</b> here</p>",
        True,
    ),
    (
        "look.xlf",
        XLF,
        "en-US",
        [f"Use {tok('x1')} and {tok('g1')}bold{tok('/g1')} now"],
        (
            '<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2"><file '
            'source-language="en" target-language="fr-FR" datatype="plaintext" original="a"><body>'
            f'<trans-unit id="1"><source>Use {tok("x1")} and <g id="1">bold</g> now</source>'
            '<target>X Use <g id="1">bold</g> and bold now</target></trans-unit></body></file>'
            "</xliff>"
        ).encode(),
        True,
    ),
    (
        "look.mdx",
        f"Use {tok('x1')} and <Kbd>Ctrl</Kbd> here\n".encode(),
        "en-US",
        [f"Use {tok('x1')} and {tok('g1')}Ctrl{tok('/g1')} here"],
        b"X Use <Kbd> and <Kbd>Ctrl</Kbd> here\n",
        True,
    ),
    (
        "look.vtt",
        f"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nUse {tok('x1')} and <b>bold</b> now\n".encode(),
        "en-US",
        [f"Use {tok('x1')} and {tok('g1')}bold{tok('/g1')} now"],
        (
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n" + f"X Use {tok('x1')} and <b>bold</b> now\n"
        ).encode(),
        True,
    ),
    (
        "look.srt",
        f"1\n00:00:01,000 --> 00:00:02,000\nUse {tok('x1')} {{\\i1}}now\n".encode(),
        "en-US",
        [f"Use {tok('x1')} {tok('x1')}now"],
        b"1\n00:00:01,000 --> 00:00:02,000\nX Use {\\i1} now\n",
        True,
    ),
    (
        "runs.docx",
        _docx(),
        "en-US",
        [f"{tok('r1')}Use {tok('/r1')}{tok('r2')}bold{tok('/r2')}{tok('r3')} now.{tok('/r3')}"],
        [[("X ", False), ("Use ", False), ("bold", True), (" now.", False)]],
        False,  # DOCX reading did not change (build-side #814.1 only)
    ),
]


@contextlib.contextmanager
def _old_rules():
    """The reading rules before read_gen, for making an "old" document by ingest. Such a
    document was made by the Python filter: the Bilingua switch is off for the block (W5 L3)."""
    t = base._LEGACY_READ.set(True)
    try:
        with bilingua_switch_off():
            yield
    finally:
        base._LEGACY_READ.reset(t)


@contextlib.contextmanager
def _no_version_check():
    """The export with the read_gen check switched off (a stored document read by the new rules)."""
    saved = base._legacy_reading
    base._legacy_reading = lambda inner: inner
    try:
        yield
    finally:
        base._legacy_reading = saved


def _units(s, did):
    q = select(Segment).where(Segment.document_id == did).order_by(Segment.id)
    return [g.source_text for g in s.exec(q)]


def _export(s, doc, name):
    from app.services.export import build_translated_file

    try:
        out = build_translated_file(s, doc, "fr-FR")
    except Exception as exc:  # a refusal is a difference too
        return f"refused: {type(exc).__name__}"
    if name.endswith(".docx"):
        import docx

        d = docx.Document(io.BytesIO(out))
        return [[(r.text, bool(r.bold)) for r in p.runs] for p in d.paragraphs]
    return out


def test_old_projects():
    from app.services.ingest import ingest_document, reingest_document

    for i, (name, data, src, units, export, versioned) in enumerate(CASES):
        with Session(engine) as s:
            p = Project(name=f"readgen-{i}", org_id=1, source_locale=src, default_provider="mock")
            s.add(p)
            s.commit()
            s.refresh(p)
            s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
            s.commit()
            with _old_rules():
                doc = ingest_document(s, p, name, data, "segmented")
            did = doc.id
            assert base.READ_GEN_KEY not in (doc.skeleton or {}), (name, doc.skeleton)
            assert _units(s, did) == units, (name, ascii(_units(s, did)))
        # repair (re-ingest) re-reads the original the old way: the same units, still no stamp
        assert reingest_document(did) is True, name
        with Session(engine) as s:
            doc = s.get(Document, did)
            assert _units(s, did) == units, (name, "reingest", ascii(_units(s, did)))
            assert base.READ_GEN_KEY not in (doc.skeleton or {}), (name, doc.skeleton)
            for g in s.exec(select(Segment).where(Segment.document_id == did)).all():
                g.target_text = "X " + g.source_text
                g.status = "translated"
                s.add(g)
            s.commit()
            got = _export(s, doc, name)
            if export is not None:
                assert got == export, (name, ascii(got))
            if versioned:
                with _no_version_check():
                    unchecked = _export(s, doc, name)
                assert unchecked != got, (name, "same without the read_gen check", ascii(got))
        print(f"  [{name}] old document repairs and exports as before OK")


WXL = (
    b'<WixLocalization Culture="en-us" xmlns="http://wixtoolset.org/schemas/v3/wxl">\r\n'
    b'  <String Id="Back"><!-- _locID_text="Back" -->&amp;Back</String>\r\n'
    b'  <String Id="Next">Next</String>\r\n'
    b"</WixLocalization>\r\n"
)


def _wxl_project(s, i):
    p = Project(name=f"readgen-wxl-{i}", org_id=1, source_locale="en-US", default_provider="mock")
    s.add(p)
    s.commit()
    s.refresh(p)
    s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
    s.commit()
    return p


def _translate_all(s, did):
    for g in s.exec(select(Segment).where(Segment.document_id == did)).all():
        g.target_text = "X " + g.source_text
        g.status = "translated"
        s.add(g)
    s.commit()


def test_wxl_comment_strings_g13():
    """g13: a wxl String with a comment inside is a unit only for a document imported with the
    stamp. An old one repairs to the same units and exports the same bytes; a new one keeps its
    stamp through repair and exports the string with the comment kept."""
    from app.services.ingest import ingest_document, reingest_document

    old_export = WXL.replace(b">Next<", b">X Next<").replace(b'Culture="en-us"', b'Culture="fr-fr"')
    with Session(engine) as s:
        with _old_rules():
            doc = ingest_document(s, _wxl_project(s, 0), "old.wxl", WXL, "segmented")
        did = doc.id
        assert base.READ_GEN_KEY not in (doc.skeleton or {}), doc.skeleton
        assert _units(s, did) == ["Next"], _units(s, did)
    assert reingest_document(did) is True
    with Session(engine) as s:
        doc = s.get(Document, did)
        assert _units(s, did) == ["Next"], ("reingest", _units(s, did))
        assert base.READ_GEN_KEY not in (doc.skeleton or {}), doc.skeleton
        _translate_all(s, did)
        got = _export(s, doc, "old.wxl")
        assert got == old_export, got
    # without the read_gen check the repair of an old document would add the comment string
    with Session(engine) as s:
        with _old_rules():
            doc = ingest_document(s, _wxl_project(s, 1), "old2.wxl", WXL, "segmented")
        did2 = doc.id
    with _no_version_check():
        assert reingest_document(did2) is True
    with Session(engine) as s:
        assert _units(s, did2) == ["&Back", "Next"], _units(s, did2)
    # a new import is stamped, keeps the stamp through repair, and writes the string
    with Session(engine) as s:
        # Python filter path (documents imported before Bilingua, or no wheel)
        with bilingua_switch_off():
            doc = ingest_document(s, _wxl_project(s, 2), "new.wxl", WXL, "segmented")
        did3 = doc.id
        assert (doc.skeleton or {}).get(base.READ_GEN_KEY) == base.READ_GEN, doc.skeleton
    assert reingest_document(did3) is True
    with Session(engine) as s:
        doc = s.get(Document, did3)
        assert (doc.skeleton or {}).get(base.READ_GEN_KEY) == base.READ_GEN, doc.skeleton
        assert _units(s, did3) == ["&Back", "Next"], _units(s, did3)
        _translate_all(s, did3)
        got = _export(s, doc, "new.wxl")
        assert got == old_export.replace(b"-->&amp;Back<", b"-->X &amp;Back<"), got
    print("  [wxl] old document repairs and exports as before; new one keeps its stamp (g13) OK")


def main():
    test_wxl_comment_strings_g13()
    test_old_projects()
    print("READ GEN OLD PROJECTS OK")


if __name__ == "__main__":
    main()
