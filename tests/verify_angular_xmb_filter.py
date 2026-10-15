"""Angular i18n XMB / XTB filter: app/filters/angular_xmb_filter.py.

  units      one unit per <msg>; <source> skipped; <ph> is a protected token, START_/CLOSE_
             pairs are paired tokens; desc/meaning/examples become the segment note.
  xtb        an XMB exports as an XTB: lang set, every distinct id once and byte-exact,
             placeholder-only messages copied, empty <ph name/> placeholders.
  identity   an uploaded XTB rebuilds byte-identical with no changes (BOM, CRLF too).
  splice     a translated XTB changes only translation content and lang.
  tolerant   unknown / repeated / stray tokens dropped, an unclosed pair closed.
  hostile    ENTITY (internal, parameter), external DTD, wrong root, broken XML, budget,
             segment cap: all refused.
  e2e        upload .xmb, API download is <stem>.<locale>.xtb, Segment notes carry desc.
  xliff      Angular `ng extract-i18n` XLIFF 1.2 and 2.0 fixtures round-trip through the
             existing XLIFF filter (target-language / trgLang, <x>/<ph>/<pc> intact).

Hermetic + offline. Final line: "ANGULAR XMB FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_angxmb_")

from lxml import etree

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError


def _read(name: str) -> bytes:
    # autocrlf may check fixtures out with CRLF on Windows; the suite pins LF bytes
    # (and tests the CRLF variant explicitly).
    with open(os.path.join(_ROOT, name), "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n")


XMB = _read("tests/fixtures/formats/angular/messages.xmb")
XMB_LOCALIZE = _read("tests/fixtures/formats/angular/messages-localize.xmb")
XTB = _read("tests/fixtures/formats/angular/messages.fr.xtb")


def _xmb():
    return get_filter("xmb")


def _units(data, fmt="xmb"):
    return get_filter(fmt).parse(data).units


def test_registered():
    assert "xmb" in supported_exts() and "xtb" in supported_exts()
    assert type(_xmb()).__name__ == "XmbFilter" and type(get_filter("xtb")).__name__ == "XtbFilter"
    from app.services.mime import export_ext_for, mime_for_fmt

    assert export_ext_for("xmb") == "xtb" and export_ext_for("XMB") == "xtb"
    assert export_ext_for("xtb") == "xtb" and export_ext_for("docx") == "docx"
    assert mime_for_fmt("xmb") == "application/xml"
    from app.services.delivery import file_name_for

    assert file_name_for("messages.xmb", "xmb", "fr") == "messages_fr.xtb"
    assert file_name_for("messages.fr.xtb", "xtb", "de") == "messages.fr_de.xtb"
    print("  [reg] .xmb/.xtb registered; xmb exports as .xtb OK")


def test_units_tokens_notes():
    pr = _xmb().parse(XMB)
    by = {u["p_index"]: u for u in pr.skeleton["units"]}
    # msg 5 is placeholder-only, msg 7 a duplicate id: neither is a unit
    assert sorted(by) == [0, 1, 2, 3, 4, 6], sorted(by)
    assert by[0]["text"] == "Hello i18n!"
    assert "src/app" not in "".join(u["text"] for u in by.values())  # <source> skipped
    assert by[0]["note"] == (
        "Description: An introduction header for this sample | Meaning: User welcome"
    )
    assert by[1]["text"] == "Hello, ⟦x1⟧! You have ⟦x2⟧ new messages."
    assert "INTERPOLATION = {{ user.name }}" in by[1]["note"]
    assert by[2]["text"] == "The ⟦g1⟧sky⟦/g1⟧ is blue & ⟦g2⟧clear⟦/g2⟧⟦x3⟧today.", by[2]
    assert by[4]["text"].startswith("{VAR_PLURAL, plural, =0 {just now}")  # ICU stays text
    assert by[6]["text"] == "Save changes"  # edge whitespace kept out of the unit
    loc = _units(XMB_LOCALIZE)
    assert loc[0].text == "You have ⟦x1⟧ items in ⟦g2⟧your cart⟦/g2⟧.", loc[0].text
    print("  [units] ph tokens, START/CLOSE pairs, source skipped, desc/meaning note OK")


def test_xmb_exports_xtb():
    f = _xmb()
    units = f.parse(XMB).units
    mapping = {str(u.p_index): "FR " + u.text for u in units}
    out = f.build_localized(XMB, mapping, "fr-CA")
    root = etree.fromstring(out)
    assert root.tag == "translationbundle" and root.get("lang") == "fr-CA"
    ids = [t.get("id") for t in root]
    assert ids == [
        "introductionHeader",
        "2367646551098052009",
        "5811701742971715242",
        "4606963464835766483",
        "2002272803511843863",
        "3967965900462880190",
        "custom.save",
    ], ids  # XMB order, duplicates once, ids byte-exact
    t = {el.get("id"): el for el in root}
    assert [p.get("name") for p in t["5811701742971715242"].iter("ph")] == [
        "START_BOLD_TEXT",
        "CLOSE_BOLD_TEXT",
        "START_LINK",
        "CLOSE_LINK",
        "LINE_BREAK",
    ]
    assert all(len(p) == 0 and not p.text for p in root.iter("ph"))  # no <ex> in XTB
    assert t["5811701742971715242"].text == "FR The "
    assert b"is blue &amp; " in out
    assert [p.get("name") for p in t["3967965900462880190"]] == ["PH"]  # copied as is
    assert t["custom.save"].text.strip() == "FR Save changes"
    assert out.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE translationbundle>')
    # untranslated messages fall back to the source text; build() has no lang
    plain = etree.fromstring(f.build(XMB, {}))
    assert plain.get("lang") is None and plain[0].text == "Hello i18n!"
    # a reordered pair and a moved point token survive
    out2 = f.build_localized(XMB, {"2": "⟦x3⟧Le ⟦g2⟧ciel⟦/g2⟧ ⟦g1⟧bleu⟦/g1⟧"}, "fr")
    t2 = etree.fromstring(out2).find("translation[@id='5811701742971715242']")
    assert [p.get("name") for p in t2.iter("ph")] == [
        "LINE_BREAK",
        "START_LINK",
        "CLOSE_LINK",
        "START_BOLD_TEXT",
        "CLOSE_BOLD_TEXT",
    ]
    print("  [xtb] XMB -> XTB: lang, exact ids, empty ph, fallback, reorder OK")


def test_xtb_identity_and_splice():
    f = get_filter("xtb")
    assert f.build(XTB, {}) == XTB
    units = f.parse(XTB).units
    assert f.build(XTB, {str(u.p_index): u.text for u in units}) == XTB
    for variant in (b"\xef\xbb\xbf" + XTB, XTB.replace(b"\n", b"\r\n")):
        assert f.build(variant, {}) == variant
    out = f.build_localized(XTB, {"1": "Bonjour, ⟦x1⟧ ! ⟦x2⟧ nouveaux."}, "fr")
    assert b"<translationbundle lang='fr'>" in out, out
    assert (
        b'<translation id="2367646551098052009">Bonjour, <ph name="INTERPOLATION"/> ! '
        b'<ph name="INTERPOLATION_1" /> nouveaux.</translation>' in out
    )
    # everything else is byte-identical
    assert (
        out.replace(b"lang='fr'", b"lang='en'").replace(
            b'Bonjour, <ph name="INTERPOLATION"/> ! <ph name="INTERPOLATION_1" /> nouveaux.',
            b'Hello, <ph name="INTERPOLATION"/>! You have <ph name="INTERPOLATION_1" />'
            b" new messages.",
        )
        == XTB
    )
    # whitespace edges kept; a missing lang is added; a self-closed translation is opened
    nolang = XTB.replace(b" lang='en'", b"")
    out = f.build_localized(nolang, {"5": "Enregistrer"}, "fr")
    assert b'<translationbundle lang="fr">' in out
    assert b'<translation id="custom.save">\n      Enregistrer\n  </translation>' in out
    one = (
        b'<?xml version="1.0"?><translationbundle><translation id="a">Hi</translation>'
        b'<translation id="b"/></translationbundle>'
    )
    assert f.build(one, {"0": "Salut & co"}) == one.replace(b">Hi<", b">Salut &amp; co<")
    print("  [splice] XTB identity (BOM/CRLF), splice, lang switch/add OK")


def test_tolerant_tokens():
    f = _xmb()
    out = f.build(XMB, {"2": "A ⟦g1⟧b ⟦x9⟧ ⟦x3⟧ ⟦x3⟧ ⟦/g7⟧ ⟦g2⟧c"})
    t = etree.fromstring(out).find("translation[@id='5811701742971715242']")
    names = [p.get("name") for p in t.iter("ph")]
    assert names == [
        "START_BOLD_TEXT",
        "LINE_BREAK",
        "START_LINK",
        "CLOSE_LINK",
        "CLOSE_BOLD_TEXT",
    ], names
    assert "⟦" not in "".join(t.itertext())
    print("  [tolerant] unknown/repeated/stray tokens dropped, unclosed pairs closed OK")


def _refused(data, exc=ValueError):
    try:
        _xmb().parse(data)
    except exc as e:
        return str(e)
    raise AssertionError(f"not refused: {data[:120]!r}")


def test_hostile():
    lol = (
        b'<?xml version="1.0"?><!DOCTYPE messagebundle [<!ENTITY a "aaaa">'
        b'<!ENTITY b "&a;&a;&a;">]><messagebundle><msg id="1">&b;</msg></messagebundle>'
    )
    assert "ENTITY" in _refused(lol)
    pe = (
        b'<?xml version="1.0"?><!DOCTYPE messagebundle [<!ENTITY % p SYSTEM "file:///etc/passwd">'
        b"%p;]><messagebundle/>"
    )
    assert "ENTITY" in _refused(pe)
    ext = (
        b'<?xml version="1.0"?><!DOCTYPE messagebundle SYSTEM "http://127.0.0.1/x.dtd">'
        b'<messagebundle><msg id="1">x</msg></messagebundle>'
    )
    assert "external DTD" in _refused(ext)
    assert "root element" in _refused(b"<html><msg id='1'>x</msg></html>")
    assert "well-formed" in _refused(b"<messagebundle><msg id='1'>x</messagebundle>")
    assert "well-formed" in _refused(b"<messagebundle><msg id='1'>&nbsp;</msg></messagebundle>")
    old_cap = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        many = (
            b"<messagebundle>"
            + b"".join(b'<msg id="%d">m%d</msg>' % (i, i) for i in range(5))
            + b"</messagebundle>"
        )
        _refused(many, InputLimitError)
        _refused(b"<messagebundle>" + b"<x/>" * 200 + b"</messagebundle>", InputLimitError)
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old_cap
    # the Angular internal-subset DOCTYPE itself is accepted and kept (never resolved)
    assert _units(XMB) and b"<!ATTLIST msg desc CDATA #IMPLIED>" in XMB
    print("  [hostile] ENTITY / parameter entity / external DTD / root / caps refused OK")


def test_e2e_upload_download():
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.main import app
    from app.models import Customer, Project, Role, Segment, SegStatus, TargetLocale, TransMode
    from app.services import apikeys, security
    from app.services.ingest import ingest_document

    init_db()
    pw = "angxmb-pw-123456"
    user = security.create_user("angxmb@x.com", "angxmb@x.com", Role.ADMIN, pw)
    _row, tok = apikeys.create_key(user.id, "angxmb", scopes="api docs keys admin")
    with Session(engine) as s:
        c = Customer(name="AngXmb Cust")
        s.add(c)
        s.commit()
        s.refresh(c)
        p = Project(
            name="angxmb",
            org_id=1,
            source_locale="en-US",
            default_provider="mock",
            customer_id=c.id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        d = ingest_document(s, p, "messages.xmb", XMB_LOCALIZE, TransMode.SEGMENTED)
        s.commit()
        s.refresh(d)
        doc_id = d.id
        segs = s.exec(
            select(Segment)
            .where(Segment.document_id == d.id, Segment.target_locale == "fr-FR")
            .order_by(Segment.order_index)
        ).all()
        assert (segs[0].meta or {}).get("notes", "").startswith("Description: Cart summary"), segs[
            0
        ].meta
        for seg in segs:
            seg.target_text = "FR " + seg.source_text
            seg.status = SegStatus.MT
            s.add(seg)
        s.commit()
    r = TestClient(app, raise_server_exceptions=False).get(
        f"/api/documents/{doc_id}/download",
        params={"locale": "fr-FR"},
        headers={"X-Loc-Token": tok},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    assert "messages.fr-FR.xtb" in r.headers.get("content-disposition", ""), r.headers
    root = etree.fromstring(r.content)
    assert root.tag == "translationbundle" and root.get("lang") == "fr-FR"
    assert root[0].get("id") == "1795488914424217085"
    assert root[0].text.startswith("FR You have") and root[1].text == "FR Checkout"
    print("  [e2e] upload .xmb -> download messages.fr-FR.xtb, notes on segments OK")


def test_angular_xliff_round_trip():
    f = get_filter("xlf")
    x1 = _read("tests/fixtures/formats/angular/messages.xlf")
    units = f.parse(x1).units
    # #693: INTERPOLATION placeholders move freely; START_/CLOSE_ markup sides keep x
    assert units[1].text == "Hello, ⟦p1⟧! You have ⟦p2⟧ new messages."
    assert units[2].text == "The ⟦x1⟧sky⟦x2⟧ is blue", units[2].text
    out = f.build_localized(x1, {str(u.p_index): "FR " + u.text for u in units}, "fr")
    ns = {"x": "urn:oasis:names:tc:xliff:document:1.2"}
    root = etree.fromstring(out)
    assert root.find("x:file", ns).get("target-language") == "fr"
    tu = root.find(".//x:trans-unit[@id='5811701742971715242']", ns)
    tgt = tu.find("x:target", ns)
    xs = [(e.get("id"), e.get("ctype"), e.get("equiv-text")) for e in tgt]
    assert xs == [("START_BOLD_TEXT", "x-b", "<b>"), ("CLOSE_BOLD_TEXT", "x-b", "</b>")], xs
    icu = root.find(".//x:trans-unit[@id='4606963464835766483']/x:target/x:x", ns)
    assert icu.get("xid") == "2002272803511843863"
    assert len(root.findall(".//x:note", ns)) == 2  # description + meaning kept
    x2 = _read("tests/fixtures/formats/angular/messages.xlf2.xlf")
    units = f.parse(x2).units
    assert units[2].text == "The ⟦g1⟧sky⟦/g1⟧ is blue", units[2].text
    out = f.build_localized(x2, {str(u.p_index): "FR " + u.text for u in units}, "fr")
    ns2 = {"x": "urn:oasis:names:tc:xliff:document:2.0"}
    root = etree.fromstring(out)
    assert root.get("trgLang") == "fr" and root.get("srcLang") == "en-US"
    pc = root.find(".//x:unit[@id='5811701742971715242']//x:target/x:pc", ns2)
    assert (pc.get("equivStart"), pc.get("equivEnd"), pc.text) == (
        "START_BOLD_TEXT",
        "CLOSE_BOLD_TEXT",
        "sky",
    )
    ph = root.find(".//x:unit[@id='2367646551098052009']//x:target/x:ph", ns2)
    assert (ph.get("id"), ph.get("equiv"), ph.get("disp")) == (
        "0",
        "INTERPOLATION",
        "{{ user.name }}",
    )
    print("  [xliff] Angular XLIFF 1.2 + 2.0 round-trip through the XLIFF filter OK")


def main():
    test_registered()
    test_units_tokens_notes()
    test_xmb_exports_xtb()
    test_xtb_identity_and_splice()
    test_tolerant_tokens()
    test_hostile()
    test_angular_xliff_round_trip()
    test_e2e_upload_download()
    print("ANGULAR XMB FILTER OK")


if __name__ == "__main__":
    main()
