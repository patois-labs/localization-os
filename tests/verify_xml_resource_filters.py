"""XML software-resource filters (file-format Batch 2): Android strings.xml and Java XML
properties (.xml), .NET .resx/.resw, WiX .wxl, Qt Linguist .ts.

Per format: the units a translator sees, a byte-identical rebuild with no translations, a
translated rebuild that changes only the translated values (the rest byte-identical), the
format's own escaping, the skip-but-count rules, build_localized (plurals / numerus, target
language stamps), the records() key view, and hostile inputs (DTD internal subset, entities,
deep nesting, size budget, not-XML, unknown .xml root). Fixtures are hand-written, under
tests/fixtures/formats/{android,resx,javaxml,wxl,ts}/.

Hermetic + offline. Final line: "XML RESOURCE FILTERS OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_xmlres_")

from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError

try:  # format Batch 7 (generic XML + ITS 2.0) may land before or after this batch
    import app.filters.xml_its  # noqa: F401

    _HAS_ITS = True
except ImportError:
    _HAS_ITS = False

FIX = os.path.join(_ROOT, "tests", "fixtures", "formats")


def _read(*parts) -> bytes:
    # LF on every checkout (git autocrlf gives CRLF on Windows); CRLF is tested explicitly.
    with open(os.path.join(FIX, *parts), "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n")


def _units(filt, data):
    return {u.p_index: u.text for u in filt.parse(data).units}


def _qt():
    from app.filters import base

    return base._FILTERS["ts"]


def _refused(filt, data, needle=""):
    try:
        filt.parse(data)
    except (ValueError, InputLimitError) as exc:
        assert needle.lower() in str(exc).lower(), (needle, str(exc))
        return str(exc)
    raise AssertionError("expected a refusal")


def _only_changed(before: bytes, after: bytes, olds: list, news: list):
    """Every byte outside the translated values is identical: substituting the new values
    back with the old ones in `after` gives `before` exactly."""
    out = after.decode("utf-8")
    for old, new in zip(olds, news, strict=True):
        assert new in out, (new, out)
        out = out.replace(new, old, 1)
    assert out.encode("utf-8") == before, "bytes outside the translated values changed"


def test_registration():
    exts = set(supported_exts())
    for e in ("xml", "resx", "resw", "wxl", "ts"):
        assert e in exts, e
    assert get_filter("resw").fmt == "resx"
    with open(os.path.join(_ROOT, "app", "templates", "project.html"), encoding="utf-8") as fh:
        html = fh.read()
    assert ".xml,.resx,.resw,.wxl,.ts" in html
    print("  [wiring] xml/resx/resw/wxl/ts registered and accepted by the upload form OK")


def test_android_units_and_skips():
    f = get_filter("xml")
    data = _read("android", "strings.xml")
    texts = list(_units(f, data).values())
    assert 'Can\'t open the file "%s".' in texts, texts  # \' and \" shown unescaped
    assert "@home is the default tag" in texts  # \@ literal
    assert "Northwind" not in texts  # translatable="false"
    assert not any(t.startswith(("@string", "?android")) for t in texts)  # references
    assert "MRC" not in texts  # string-array translatable="false"
    assert "#FF4081" not in texts and "16dp" not in texts  # not string resources
    assert "First line\\nSecond line" in texts  # \n kept as typed
    assert "It's   spaced   out" in texts  # double-quoted value, quotes dropped
    styled = next(t for t in texts if t.startswith("Tap "))
    assert styled == "Tap ⟦g1⟧Save⟦/g1⟧ to keep ⟦g2⟧your⟦/g2⟧ changes."
    ph = next(t for t in texts if t.startswith("Signed in"))
    assert ph == "Signed in as ⟦x1⟧.", ph  # xliff:g is a protected point token
    assert "%1$s" not in ph
    assert f.build(data, {}) == data
    print("  [android] escapes unescaped for the translator, skips counted, identity build OK")


def test_android_escaping_on_build():
    f = get_filter("xml")
    data = _read("android", "strings.xml")
    units = _units(f, data)
    idx = {t: i for i, t in units.items()}
    tr = {
        str(idx['Can\'t open the file "%s".']): 'Impossible d\'ouvrir "%s".',
        str(idx["@home is the default tag"]): "@accueil est l'étiquette",
        str(idx["It's   spaced   out"]): "C'est   espacé",
        str(idx["First line\\nSecond line"]): "Première\\nSeconde",
        str(next(i for i, t in units.items() if t.startswith("Signed in"))): "Connecté : ⟦x1⟧.",
        str(next(i for i, t in units.items() if t.startswith("Tap "))): (
            "Touchez ⟦g1⟧Enregistrer⟦/g1⟧ pour garder ⟦g2⟧vos⟦/g2⟧ modifications d'abord."
        ),
        str(next(i for i, t in units.items() if t.startswith("Read the"))): "Lisez d'abord.",
    }
    out = f.build(data, tr).decode("utf-8")
    assert '<string name="cant_open">Impossible d\\\'ouvrir \\"%s\\".</string>' in out, out
    assert '<string name="at_literal">\\@accueil est l\\\'étiquette</string>' in out, out
    assert '<string name="quoted">"C\\\'est   espacé"</string>' in out, out
    assert "Première\\nSeconde</string>" in out
    assert '<xliff:g id="user" example="Ana">%1$s</xliff:g>' in out, out
    assert "xmlns:xliff" not in out.split("<resources", 1)[1].split(">", 1)[1], "ns re-declared"
    assert "<b>Enregistrer</b> pour garder <i>vos</i> modifications d\\'abord." in out, out
    assert "<![CDATA[Lisez d\\'abord.]]>" in out, out
    # untranslated entries and every other byte stay as they were
    assert '<string name="welcome">Welcome, %1$s! You have %2$d new messages.</string>' in out
    assert '<string name="alias">@string/welcome</string>' in out
    # the rebuilt file parses again and shows the translator the same text
    again = list(_units(f, out.encode("utf-8")).values())
    assert 'Impossible d\'ouvrir "%s".' in again and "@accueil est l'étiquette" in again, again
    # an unescaped apostrophe never survives into a value (aapt2 would refuse the build)
    import re

    for m in re.finditer(r"<string name=\"[^\"]+\">(.*?)</string>", out):
        v = m.group(1)
        if v.startswith('"'):
            continue
        assert not re.search(r"(?<!\\)'", v), v
    print("  [android] apostrophes, quotes, leading @, CDATA, quoted values, inline tags OK")


def test_android_raw_line_break_706():
    """#706: the engine answers a `\\n` escape with a REAL line break (live de/fr/it); aapt2
    reads a raw break in an unquoted value as one space, so the line break was lost. The
    build writes it back as `\\n` when the source used `\\n` and has no raw break."""
    f = get_filter("xml")
    data = (
        b'<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        b'    <string name="two">Your download is complete.\\nOpen the file.</string>\n'
        b'    <string name="kept">One\\nTwo</string>\n'
        b'    <string name="plain">No break here.</string>\n'
        b'    <plurals name="left">\n'
        b'        <item quantity="one">%d file left.\\nKeep going.</item>\n'
        b'        <item quantity="other">%d files left.\\nKeep going.</item>\n'
        b"    </plurals>\n</resources>\n"
    )
    units = _units(f, data)
    idx = {t: i for i, t in units.items()}
    tr = {
        str(idx["Your download is complete.\\nOpen the file."]): (
            "Ihr Download ist abgeschlossen.\nÖffnen Sie die Datei."
        ),
        str(idx["One\\nTwo"]): "Eins\\n\nZwei",  # escape kept AND a break added: one escape
        str(idx["No break here."]): "Hier kein\nUmbruch.",  # no escape in the source: as is
        str(idx["%d file left.\\nKeep going."]): "Noch %d Datei.\r\n  Weiter so.",
        str(idx["%d files left.\\nKeep going."]): "Noch %d Dateien.\\nWeiter so.",
    }
    out = f.build(data, tr).decode("utf-8")
    assert "Ihr Download ist abgeschlossen.\\nÖffnen Sie die Datei.</string>" in out, out
    assert '<string name="kept">Eins\\nZwei</string>' in out, out
    assert "Hier kein\nUmbruch.</string>" in out, out
    assert "Noch %d Datei.\\nWeiter so.</item>" in out, out
    assert "Noch %d Dateien.\\nWeiter so.</item>" in out, out
    print("  [android] a raw line break for a \\n escape is written back as \\n (#706) OK")


def test_android_plurals_localized():
    f = get_filter("xml")
    data = _read("android", "strings.xml")
    units = _units(f, data)
    other = next(i for i, t in units.items() if t == "%d songs found.")
    out = f.build_localized(data, {str(other): "Znaleziono %d utworów."}, "pl").decode("utf-8")
    block = out.split('<plurals name="songs">', 1)[1].split("</plurals>", 1)[0]
    for q in ("one", "few", "many", "other"):
        assert f'quantity="{q}"' in block, (q, block)
    assert block.count("Znaleziono %d utworów.") == 3, block  # other + new few + new many
    ja = f.build_localized(data, {}, "ja").decode("utf-8")
    block = ja.split('<plurals name="files">', 1)[1].split("</plurals>", 1)[0]
    assert 'quantity="other"' in block and 'quantity="one"' not in block, block
    assert 'quantity="few"' not in block and 'quantity="many"' not in block, block
    # plain build() never changes the plural set
    assert f.build(data, {}) == data
    print("  [android] plurals follow the target language (pl adds few/many, ja keeps other) OK")


def test_resx():
    f = get_filter("resx")
    data = _read("resx", "Strings.resx")
    units = _units(f, data)
    assert sorted(units.values()) == [
        "Hello, {0}! You have {1} new messages.",
        "OK, continue",
        "Save & close",
    ], units
    assert f.build(data, {}) == data
    tr = {
        str(i): t.replace("Hello", "Hallo").replace("Save", "Sichern & <ok>")
        for i, t in units.items()
    }
    tr = {k: v for k, v in tr.items() if v != units[int(k)]}
    out = f.build(data, tr)
    _only_changed(
        data,
        out,
        ["Hello, {0}! You have {1} new messages.", "Save &amp; close"],
        ["Hallo, {0}! You have {1} new messages.", "Sichern &amp; &lt;ok&gt; &amp; close"],
    )
    s = out.decode("utf-8")
    assert "<value>  Sichern &amp; &lt;ok&gt; &amp; close  </value>" in s  # padding kept
    recs = {r.key: r for r in f.records(data)}
    assert recs["Greeting"].comment == "{0} is the user name, {1} a count"
    assert not recs["Logo"].translatable and not recs["Readme"].translatable
    assert not recs[">>okButton.Name"].translatable and recs["okButton.Text"].translatable
    # .resw (UWP / WinUI) is the same format; CRLF and a UTF-8 BOM survive byte for byte
    resw = b"\xef\xbb\xbf" + data.replace(b"\n", b"\r\n")
    out2 = get_filter("resw").build(resw, tr)
    assert out2.startswith(b"\xef\xbb\xbf") and b"\r\n" in out2 and b"\n\n" not in out2
    assert out2.count(b"\r\n") == resw.count(b"\r\n")
    assert get_filter("resw").build(resw, {}) == resw
    print(
        "  [resx] type/mimetype and >> metadata skipped, header+schema byte-identical, BOM/CRLF OK"
    )


def test_java_xml_properties():
    f = get_filter("xml")
    data = _read("javaxml", "messages.xml")
    units = _units(f, data)
    assert "Hello {0}, it's {1,date,long}." in units.values(), units  # '' shown as '
    assert f.build(data, {}) == data
    i = next(k for k, t in units.items() if t.startswith("Hello"))
    out = f.build(data, {str(i): "Bonjour {0}, c'est le {1,date,long}."}).decode("utf-8")
    assert "<entry key=\"greeting\">Bonjour {0}, c''est le {1,date,long}.</entry>" in out, out
    assert '<!DOCTYPE properties SYSTEM "http://java.sun.com/dtd/properties.dtd">' in out
    assert "<comment>Hand-written fixture for Localization OS</comment>" in out
    # a value without MessageFormat arguments keeps a single apostrophe
    j = next(k for k, t in units.items() if t.startswith("Goodbye"))
    out = f.build(data, {str(j): "Au revoir & merci d'avance"}).decode("utf-8")
    assert "Au revoir &amp; merci d'avance</entry>" in out, out
    assert [r.key for r in f.records(data)] == ["greeting", "farewell", "build.id"]
    print("  [javaxml] DOCTYPE kept, MessageFormat apostrophes doubled only with {n} OK")


def test_wxl():
    f = get_filter("wxl")
    data = _read("wxl", "Package.en-us.wxl")
    units = _units(f, data)
    assert "{6F2C1E7A-0000-4000-8000-000000000001}" not in units.values()  # Localizable="no"
    assert "A newer version of [ProductName] is already installed." in units.values()
    assert f.build(data, {}) == data
    tr = {
        str(k): {
            "Northwind Desktop": 'Northwind "Bureau" & co',
            "Click Next to continue.": "Klicken Sie auf Weiter.",
            "Welcome to the setup wizard": "Willkommen",
        }.get(t, t)
        for k, t in units.items()
    }
    out = f.build_localized(data, tr, "de-DE").decode("utf-8")
    assert '<String Id="ProductName" Value="Northwind &quot;Bureau&quot; &amp; co" />' in out, out
    assert '<String Id="LegacyText">Klicken Sie auf Weiter.</String>' in out  # WiX v3 form
    assert 'Text="Willkommen"' in out
    assert 'Culture="de-de" Codepage="1252"' in out, out  # Codepage left as uploaded
    again = _units(f, out.encode("utf-8"))
    assert 'Northwind "Bureau" & co' in again.values(), again
    # Gate: an attribute look-alike inside another attribute's value is left alone.
    tricky = (
        b'<WixLocalization Culture="en-us" xmlns="http://wixtoolset.org/schemas/v4/wxl">'
        b'<String Id="a Value=\'x\'" Value="Hello" /></WixLocalization>'
    )
    out = f.build(tricky, {"0": "Hallo"}).decode("utf-8")
    assert '<String Id="a Value=\'x\'" Value="Hallo" />' in out, out
    print("  [wxl] Value attribute and v3 text, Localizable=no skipped, Culture stamped OK")


def test_qt_ts():
    f = get_filter("ts")
    data = _read("ts", "app_de.ts")
    units = _units(f, data)
    assert "Old caption" not in units.values()  # vanished
    assert "&Open file..." in units.values()
    assert "Tab⟦x1⟧separated" in units.values()  # <byte> is protected
    assert f.build(data, {}) == data
    tr = {
        str(k): {
            "&Open file...": "Datei &öffnen...",
            "Saved %1 of %2 pages": "%1 von %2 Seiten gespeichert",
            "%n file(s) deleted": "%n Datei(en) gelöscht",
            "Tab⟦x1⟧separated": "Tab⟦x1⟧getrennt",
        }[t]
        for k, t in units.items()
    }
    out = f.build(data, tr).decode("utf-8")
    assert "<translation>Datei &amp;öffnen...</translation>" in out, out
    assert "<translation>%1 von %2 Seiten gespeichert</translation>" in out, out  # was <x/>
    assert out.count("<numerusform>%n Datei(en) gelöscht</numerusform>") == 2, out
    assert '<translation type="vanished">Alte Beschriftung</translation>' in out
    assert '<translation>Tab<byte value="x9"/>getrennt</translation>' in out, out
    assert out.startswith('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n')
    assert 'type="unfinished"' not in out
    # a numerus message with no forms gets one per plural form of the target (Polish: 3)
    bare = data.replace(
        b"""<translation type="unfinished">
            <numerusform></numerusform>
            <numerusform></numerusform>
        </translation>""",
        b'<translation type="unfinished"/>',
    )
    k = next(k for k, t in units.items() if t.startswith("%n"))
    pl = f.build_localized(bare, {str(k): "%n plik(ów)"}, "pl").decode("utf-8")
    assert pl.count("<numerusform>%n plik(ów)</numerusform>") == 3, pl
    assert 'language="pl"' in pl, pl
    # #729 (decision 18): a message that already has 2 forms (an English or German .ts)
    # gets the rest of the target's forms on a localized build (Polish 3, Arabic 6), each
    # with the translation (the shared rule, app/filters/plurals.py); Qt showed the
    # source for the counts of the missing forms. build() and a 1-form target add none.
    pl2 = f.build_localized(data, {str(k): "%n plik(ów)"}, "pl-PL").decode("utf-8")
    assert pl2.count("<numerusform>%n plik(ów)</numerusform>") == 3, pl2
    assert (
        "            <numerusform>%n plik(ów)</numerusform>\n"
        "            <numerusform>%n plik(ów)</numerusform>\n"
        "            <numerusform>%n plik(ów)</numerusform>\n        </translation>"
    ) in pl2, pl2
    ar = f.build_localized(data, {str(k): "AR %n"}, "ar-SA").decode("utf-8")
    assert ar.count("<numerusform>AR %n</numerusform>") == 6, ar
    ja = f.build_localized(data, {str(k): "JA %n"}, "ja-JP").decode("utf-8")
    assert ja.count("<numerusform>JA %n</numerusform>") == 2, ja
    assert f.build(data, {str(k): "X %n"}).decode("utf-8").count("<numerusform>X") == 2
    recs = f.records(data)
    assert recs[0].context == "MainWindow" and recs[0].comment == "menu entry"
    assert recs[2].plural == "numerus" and not recs[3].translatable
    print("  [ts] unfinished cleared, vanished kept, numerus forms per target, <byte> kept OK")


def test_encoding_and_line_endings():
    f = get_filter("resx")
    data = _read("resx", "Strings.resx")
    utf16 = data.decode("utf-8").replace('encoding="utf-8"', 'encoding="utf-16"').encode("utf-16")
    parsed = f.parse(utf16)
    assert parsed.skeleton.get("source_encoding") == "utf-16", parsed.skeleton
    assert len(parsed.units) == 3
    out = f.build(utf16, {str(parsed.units[0].p_index): "Hallo {0}"})
    assert out.startswith(b'<?xml version="1.0" encoding="UTF-8"?>'), out[:60]
    assert "Hallo {0}" in out.decode("utf-8")
    print("  [encoding] UTF-16 upload converted, exported as UTF-8 with a truthful declaration OK")


def test_hostile_inputs():
    from app import config

    xml = get_filter("xml")
    lol = (
        b'<?xml version="1.0"?><!DOCTYPE resources [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b'<resources><string name="x">&b;</string></resources>'
    )
    _refused(xml, lol, "internal subset")
    # Gate: a ">" inside a quoted system id must not hide the internal subset (the entity
    # here is used only in an attribute, so no entity node would be left to catch it).
    hidden = (
        b'<?xml version="1.0"?><!DOCTYPE resources SYSTEM "a>b" [<!ENTITY a "x">]>'
        b'<resources><string name="x" y="&a;">hi</string></resources>'
    )
    _refused(xml, hidden, "internal subset")
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE resources SYSTEM "file:///etc/passwd">'
        b'<resources><string name="x">&xxe;</string></resources>'
    )
    _refused(xml, xxe, "entity")
    _refused(_qt(), b"export const x: number = 1;\n", "not well-formed")
    other = b'<?xml version="1.0"?><catalog><item>Hello</item></catalog>'
    if _HAS_ITS:  # gate: an unknown root falls back to the ITS 2.0 filter, both ways
        assert [u.text for u in xml.parse(other).units] == ["Hello"]
        assert xml.build(other, {"0": "Hallo"}) == other.replace(b"Hello", b"Hallo")
        assert xml.build(other, {}) == other
    else:
        _refused(xml, other, "not a supported")
    deep = (
        b"<resources><string name='x'>"
        + b"<b>" * 3000
        + b"t"
        + b"</b>" * 3000
        + b"</string></resources>"
    )
    _refused(xml, deep)
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 5
    try:
        many = (
            b"<resources>"
            + b"".join(b'<string name="s%d">Text %d</string>' % (i, i) for i in range(20))
            + b"</resources>"
        )
        _refused(xml, many, "MAX_DOCUMENT_SEGMENTS")
        big = b"<root>" + b"<x/>" * 300 + b"</root>"
        _refused(get_filter("resx"), big, "MAX_DOCUMENT_SEGMENTS")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    # a stray ]]> in a translation for a CDATA value falls back to escaped text, still valid
    data = _read("android", "strings.xml")
    units = _units(xml, data)
    i = next(k for k, t in units.items() if t.startswith("Read the"))
    out = xml.build(data, {str(i): "a ]]> b"})
    assert "a ]]&gt; b" in out.decode("utf-8")
    xml.parse(out)
    print("  [hostile] DTD subset, entities, not-XML, unknown root, depth and size caps refused OK")


def test_upload_translate_download_end_to_end():
    """API upload -> CLI translate (offline mock engine) -> API download, for an Android
    strings.xml and a .resx: the export is the same file with translated values, escaped
    the format's way, and every non-text entry untouched."""
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    loc = "de-DE"
    init_db()
    with Session(engine) as s:
        p = Project(name="xmlres", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        pid = p.id
    user = security.create_user("xmlres@x.test", "xmlres", Role.ADMIN, "xmlres-pw-123456")
    _row, raw = apikeys.create_key(user.id, "xmlres", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key
    for name, data in (
        ("strings.xml", _read("android", "strings.xml")),
        ("Strings.resx", _read("resx", "Strings.resx")),
    ):
        r = client.post(
            "/api/documents",
            headers=hdr,
            data={"project_id": str(pid)},
            files={"file": (name, data, "application/xml")},
        )
        assert r.status_code in (200, 201), (r.status_code, r.text)
        doc = r.json()["document_id"]
        code, out, err = run_cli(["doc", "translate", str(doc), "--locale", loc])
        assert code == 0, (code, out, err)
        r = client.get(f"/api/documents/{doc}/download", headers=hdr, params={"locale": loc})
        assert r.status_code == 200, (r.status_code, r.text)
        text = r.content.decode("utf-8")
        filt = get_filter(name.rsplit(".", 1)[1])
        again = filt.parse(r.content)  # the export is a valid file of the same format
        dropped = 2 if name.endswith(".xml") else 0  # German has no few/many plural forms
        assert len(again.units) == len(filt.parse(data).units) - dropped, (name, text)
        assert "[de-DE]" in text, text  # the offline mock engine marks what it translated
        if name.endswith(".xml"):
            assert 'Can\\\'t open the file \\"%s\\".' in text, text
            assert '<string name="app_name" translatable="false">Northwind</string>' in text
            assert '<string name="alias">@string/welcome</string>' in text
            assert '<xliff:g id="user" example="Ana">%1$s</xliff:g>' in text, text
        else:
            src = data.decode("utf-8")
            head = src.split('<data name="Greeting"', 1)[0]
            assert text.startswith(head), "RESX header and schema must stay byte-identical"
            assert (
                "Qk02AAAAAAAAADYAAAAoAAAAAQAAAAEAAAABABgAAAAAAAAAAADEDgAAxA4AAAAAAAAAAAAA" in text
            )
            assert "{0}" in text and "{1}" in text, text
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("data.xml", b"<catalog><item>x</item></catalog>", "application/xml")},
    )
    if _HAS_ITS:  # any other root goes to the generic ITS 2.0 XML filter
        assert r.status_code == 200, (r.status_code, r.text)
    else:
        assert r.status_code in (400, 413, 422), (r.status_code, r.text)
        assert "not a supported resource format" in r.text, r.text
    print("  [e2e] API upload, CLI translate, API download for strings.xml and .resx OK")


def main():
    test_registration()
    test_android_units_and_skips()
    test_android_escaping_on_build()
    test_android_raw_line_break_706()
    test_android_plurals_localized()
    test_resx()
    test_java_xml_properties()
    test_wxl()
    test_qt_ts()
    test_encoding_and_line_endings()
    test_hostile_inputs()
    test_upload_translate_download_end_to_end()
    print("XML RESOURCE FILTERS OK")


if __name__ == "__main__":
    main()
