"""XAML filter (app/filters/xaml_filter.py): ResourceDictionary sys:String / x:String
resources, optional view attributes, byte-faithful round trip, hostile input.

Hand-written fixtures only. Hermetic + offline. Final line: "XAML FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_xaml_")

from lxml import etree

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import EmptyDocumentError, InputLimitError

F = get_filter("xaml")

WPF = (
    '<?xml version="1.0" encoding="utf-8"?>\r\n'
    "<!-- Strings for the main window -->\r\n"
    '<ResourceDictionary xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"\r\n'
    '                    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"\r\n'
    '                    xmlns:system="clr-namespace:System;assembly=mscorlib">\r\n'
    '    <system:String x:Key="Greeting">Hello, {0}!</system:String>\r\n'
    "    <system:String x:Key='Save' >Save &amp; close</system:String>\r\n"
    '    <system:String x:Key="Empty"></system:String>\r\n'
    '    <system:String x:Key="Dash">--</system:String>\r\n'
    '    <system:String x:Key="Bound">{Binding Name}</system:String>\r\n'
    '    <system:String x:Key="Note"><!-- todo -->Later</system:String>\r\n'
    '    <system:String x:Key="Multi">\r\n'
    "        First line &#x41;&lt;b&gt;\r\n"
    "    </system:String>\r\n"
    '    <SolidColorBrush x:Key="Accent" Color="#FF0078D7"/>\r\n'
    '    <x:Double x:Key="Size">12</x:Double>\r\n'
    "</ResourceDictionary>\r\n"
)

MAUI = (
    '<ResourceDictionary xmlns="http://schemas.microsoft.com/dotnet/2021/maui"\n'
    '    xmlns:x="http://schemas.microsoft.com/winfx/2009/xaml">\n'
    '  <x:String x:Key="Title">Welcome</x:String>\n'
    '  <x:String x:Key="Count">%1$s items</x:String>\n'
    "</ResourceDictionary>\n"
)

VIEW = (
    '<Window x:Class="App.Main" Title="Main window"\n'
    '        xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"\n'
    '        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"\n'
    '        xmlns:sys="clr-namespace:System;assembly=System.Runtime">\n'
    "  <Window.Resources>\n"
    '    <sys:String x:Key="Ok">OK</sys:String>\n'
    "  </Window.Resources>\n"
    "  <StackPanel>\n"
    '    <Button Content="Click me" ToolTip=\'Saves "all"\' Name="SaveButton"/>\n'
    '    <TextBlock Text="{Binding Status}"/>\n'
    '    <TextBlock Text="{}{0} files"/>\n'
    '    <TextBlock Text="{StaticResource Ok}" Tag="Not translated"/>\n'
    '    <HeaderedContentControl Header="Settings" Content="42"/>\n'
    "    <Button.Content>Ignored property element</Button.Content>\n"
    "  </StackPanel>\n"
    "</Window>\n"
)


def _texts(data: bytes):
    return [u.text for u in F.parse(data).units]


def _refused(data: bytes, exc=ValueError, needle: str = "") -> str:
    try:
        F.parse(data)
    except exc as e:
        assert needle.lower() in str(e).lower(), (needle, str(e))
        return str(e)
    raise AssertionError(f"expected refusal for {data[:60]!r}")


def test_registration():
    assert "xaml" in supported_exts()
    assert get_filter("xaml").fmt == "xaml"
    from app.services.connectors import msgraph

    assert msgraph.FORMATS.get("xaml") == "xaml"
    with open(os.path.join(_ROOT, "app", "templates", "project.html"), encoding="utf-8") as fh:
        html = fh.read()
    assert ".xaml," in html
    print("  [reg] xaml registered, upload accept list and msgraph map OK")


def test_wpf_resources():
    data = WPF.encode("utf-8")
    texts = _texts(data)
    assert texts == ["Hello, {0}!", "Save & close", "First line A<b>"], texts
    # no change -> byte identical
    assert F.build(data, {}) == data
    assert F.build(data, {str(i): t for i, t in enumerate(texts)}) == data
    out = F.build(data, {"0": "Hallo, {0}!", "1": "Speichern & schließen", "2": "Erste <Zeile>"})
    exp = (
        WPF.replace(">Hello, {0}!<", ">Hallo, {0}!<")
        .replace(">Save &amp; close<", ">Speichern &amp; schließen<")
        .replace("First line &#x41;&lt;b&gt;", "Erste &lt;Zeile&gt;")
    ).encode("utf-8")
    assert out == exp, out.decode()
    etree.fromstring(out)  # still well-formed
    assert _texts(out) == ["Hallo, {0}!", "Speichern & schließen", "Erste <Zeile>"]
    print("  [wpf] sys:String resources, skips, entities, CRLF, byte-identical splice OK")


def test_multiline_crlf_translation():
    data = WPF.encode("utf-8")
    out = F.build(data, {"2": "Zeile eins\nZeile zwei"}).decode("utf-8")
    assert "Zeile eins\r\nZeile zwei" in out, out
    print("  [wpf] newline in a translation follows the source CRLF convention OK")


def test_maui_x_string_and_placeholders():
    data = MAUI.encode("utf-8")
    assert _texts(data) == ["Welcome", "%1$s items"]
    out = F.build(data, {"1": "%1$s Elemente"})
    assert out == MAUI.replace("%1$s items", "%1$s Elemente").encode("utf-8")
    print("  [maui] x:String (XAML 2009) and printf placeholders survive OK")


def test_view_attributes_off_by_default():
    assert config.XAML_VIEW_ATTRIBUTES is False
    data = VIEW.encode("utf-8")
    assert _texts(data) == ["OK"]
    # a pure view with the option off is refused with a hint, not silently empty
    pure = b'<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" Title="T"/>'
    _refused(pure, EmptyDocumentError, "XAML_VIEW_ATTRIBUTES")
    print("  [view] attributes off by default; pure view refused with a hint OK")


def test_view_attributes_on():
    config.XAML_VIEW_ATTRIBUTES = True
    try:
        data = VIEW.encode("utf-8")
        texts = _texts(data)
        # resources first, then attributes in document order; markup extensions,
        # property elements, non-listed attributes and number-free values skipped
        want = ["OK", "Main window", "Click me", 'Saves "all"', "{0} files", "Settings"]
        assert texts == want, texts
        assert F.build(data, {}) == data
        out = F.build(
            data,
            {
                "0": "Gut",
                "1": "Hauptfenster",
                "2": "Klick & mich",
                "3": "Speichert 'alles' \"ganz\"",
                "4": "{0} Dateien",
                "5": "{Einstellungen}",
            },
        ).decode("utf-8")
        assert 'Title="Hauptfenster"' in out
        assert 'Content="Klick &amp; mich"' in out
        assert "ToolTip='Speichert &apos;alles&apos; \"ganz\"'" in out, out
        assert 'Text="{}{0} Dateien"' in out
        # a translation starting with a brace gets the {} escape: never a markup extension
        assert 'Header="{}{Einstellungen}"' in out, out
        assert 'Text="{Binding Status}"' in out and 'Tag="Not translated"' in out
        assert ">Gut</sys:String>" in out
        etree.fromstring(out.encode("utf-8"))
        # flipping the option off at export never misplaces a resource string
        config.XAML_VIEW_ATTRIBUTES = False
        out2 = F.build(data, {"0": "Gut", "1": "Hauptfenster"}).decode("utf-8")
        assert ">Gut</sys:String>" in out2 and 'Title="Main window"' in out2
    finally:
        config.XAML_VIEW_ATTRIBUTES = False
    print("  [view] Content/Text/ToolTip/Header/Title opt-in, {} escape, quoting OK")


def test_encodings_and_bom():
    data = WPF.encode("utf-8")
    bom = b"\xef\xbb\xbf" + data
    assert _texts(bom)[0] == "Hello, {0}!"
    assert F.build(bom, {}) == bom
    out = F.build(bom, {"0": "Hallo"})
    assert out.startswith(b"\xef\xbb\xbf") and b">Hallo<" in out
    for codec, mark in (("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")):
        u16 = mark + MAUI.encode(codec)
        parsed = F.parse(u16)
        assert [u.text for u in parsed.units] == ["Welcome", "%1$s items"]
        assert parsed.skeleton.get("source_encoding") == codec
        assert F.build(u16, {}) == u16
        out = F.build(u16, {"0": "Willkommen ✓"})
        assert out == mark + MAUI.replace(">Welcome<", ">Willkommen ✓<").encode(codec)
    # declared legacy charset: re-encoded the same way, unrepresentable -> char reference
    legacy = MAUI.replace("Welcome", "Café").encode("cp1252")
    legacy = b'<?xml version="1.0" encoding="windows-1252"?>\n' + legacy
    assert _texts(legacy)[0] == "Café"
    assert F.build(legacy, {}) == legacy
    out = F.build(legacy, {"0": "Café ✓"})
    assert b">Caf\xe9 &#10003;<" in out, out
    assert "source_encoding" not in F.parse(MAUI.encode("utf-8")).skeleton  # UTF-8: no key
    # undeclared non-UTF-8 is refused, never mojibake
    _refused(MAUI.replace("Welcome", "Café").encode("cp1252"), ValueError, "UTF-8")
    print("  [enc] UTF-8 BOM, UTF-16 LE/BE, declared windows-1252 round trips OK")


def test_hostile_inputs():
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///c:/windows/win.ini">]>'
        b'<ResourceDictionary xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">'
        b'<x:String x:Key="a">&x;</x:String></ResourceDictionary>'
    )
    _refused(xxe, ValueError, "DOCTYPE")
    lol = b'<!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><r>&b;</r>'
    _refused(lol, ValueError, "DOCTYPE")
    _refused(b"<ResourceDictionary><x:String>", ValueError, "well-formed")
    _refused(b"<a>&undefined;</a>", ValueError, "well-formed")
    _refused(b"\x00\x01binary", ValueError)
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        many = (
            '<ResourceDictionary xmlns:x="http://schemas.microsoft.com/winfx/2009/xaml">'
            + "".join(f'<x:String x:Key="k{i}">Value {i}</x:String>' for i in range(5))
            + "</ResourceDictionary>"
        ).encode()
        _refused(many, InputLimitError, "MAX_DOCUMENT_SEGMENTS")
        _refused(b"<a>" + b"<b/>" * 200 + b"</a>", InputLimitError, "MAX_DOCUMENT_SEGMENTS")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    old = config.MAX_SEGMENT_CHARS
    try:
        config.MAX_SEGMENT_CHARS = 1000
        big = (
            '<ResourceDictionary xmlns:x="http://schemas.microsoft.com/winfx/2009/xaml">'
            '<x:String x:Key="k">' + "word " * 400 + "</x:String></ResourceDictionary>"
        ).encode()
        _refused(big, InputLimitError, "MAX_SEGMENT_CHARS")
    finally:
        config.MAX_SEGMENT_CHARS = old
    print("  [hostile] XXE, billion laughs, malformed, binary, segment and char caps refused OK")


def test_illegal_xml_chars_in_translation():
    data = MAUI.encode("utf-8")
    out = F.build(data, {"0": "Bad\x01char\x0bbreak"})
    etree.fromstring(out)
    assert b">Badchar\nbreak<" in out, out
    print("  [build] XML-illegal characters stripped from targets OK")


def test_namespace_resolution():
    # a String in some other namespace, or an unprefixed String, is not a resource string
    data = (
        b'<ResourceDictionary xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"'
        b' xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"'
        b' xmlns:my="clr-namespace:MyApp" xmlns:s="using:System">'
        b'<my:String x:Key="a">Not me</my:String><String>Nor me</String>'
        b'<s:String x:Key="b">Uwp string</s:String>'
        b'<Grid xmlns:s="clr-namespace:Other"><s:String>Rebound</s:String></Grid>'
        b"</ResourceDictionary>"
    )
    assert _texts(data) == ["Uwp string"], _texts(data)
    print("  [ns] prefix scoping and the System namespace forms OK")


def test_api_and_cli_round_trip():
    """Upload a .xaml through the API and the CLI, mock-translate, export: the file comes
    back well-formed with only the resource strings changed."""
    import tempfile

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
        p = Project(name="xaml", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        pid = p.id
    user = security.create_user("xaml@x.test", "xaml", Role.ADMIN, "xaml-pw-123456")
    _row, raw_key = apikeys.create_key(user.id, "xaml", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw_key}  # pregate: allow-secret throwaway test key
    data = WPF.encode("utf-8")

    def check(out: bytes, surface: str):
        etree.fromstring(out)
        text = out.decode("utf-8")
        assert f">[{loc}] Hello, {{0}}!<" in text, (surface, text)
        assert f">[{loc}] Save &amp; close<" in text, (surface, text)
        assert '<SolidColorBrush x:Key="Accent" Color="#FF0078D7"/>' in text, (surface, text)
        assert ">{Binding Name}<" in text and "\r\n" in text, (surface, text)

    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("Strings.xaml", data, "application/xaml+xml")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc), "--locale", loc])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc}/download", headers=hdr, params={"locale": loc})
    assert r.status_code == 200, (r.status_code, r.text)
    check(r.content, "api")
    r = client.get("/api/formats", headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text)
    assert {"ext": "xaml", "name": "XAML resources"} in r.json()["formats"], r.text

    tmp = tempfile.mkdtemp(prefix="loctms_xaml_cli_")
    src = os.path.join(tmp, "Strings.xaml")
    with open(src, "wb") as fh:
        fh.write(data)
    code, out, err = run_cli(["doc", "upload", str(pid), src])
    assert code == 0, (code, out, err)
    import re

    m = re.search(r"Uploaded #(\d+): Strings\.xaml \(xaml,", out)
    assert m, out
    cli_doc = int(m.group(1))
    code, out, err = run_cli(["doc", "translate", str(cli_doc), "--locale", loc])
    assert code == 0, (code, out, err)
    dest = os.path.join(tmp, "out.xaml")
    code, out, err = run_cli(["doc", "export", str(cli_doc), "--locale", loc, "-o", dest])
    assert code == 0, (code, out, err)
    with open(dest, "rb") as fh:
        check(fh.read(), "cli")
    print("  [surfaces] API upload/download and CLI upload/export round trip OK")


def main():
    test_registration()
    test_wpf_resources()
    test_multiline_crlf_translation()
    test_maui_x_string_and_placeholders()
    test_view_attributes_off_by_default()
    test_view_attributes_on()
    test_encodings_and_bom()
    test_hostile_inputs()
    test_illegal_xml_chars_in_translation()
    test_namespace_resolution()
    test_api_and_cli_round_trip()
    print("XAML FILTER OK")


if __name__ == "__main__":
    main()
