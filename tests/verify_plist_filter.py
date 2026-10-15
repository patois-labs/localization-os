"""Apple property-list filters (file-format Batch 8): `.plist` and `.stringsdict`.

  identity   an unchanged build returns the original bytes (XML, CRLF, UTF-16, binary)
  splice     a translated XML build changes only the translated <string> contents
  policy     Info.plist / Settings.bundle / generic plist / stringsdict pick the right strings
  tokens     %@ %lld %1$#@var@ become ⟦xN⟧ tokens and come back on build
  plurals    stringsdict build_localized adds the target's CLDR categories (ru few/many)
  records    the key-based view (Batch 9)
  hostile    DTD entities, XXE, undefined entities, OpenStep, cycles, depth, caps
  surfaces   web upload -> translate -> export of a .stringsdict

Hermetic + offline; fixtures are hand-written under tests/fixtures/formats/apple/.
Final line: "PLIST FILTER OK".
"""

import os
import plistlib
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_plist_")

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError

FX = os.path.join(_ROOT, "tests", "fixtures", "formats", "apple")
PH = "\u27e6x1\u27e7"


def _fx(name: str) -> bytes:
    with open(os.path.join(FX, name), "rb") as fh:
        return fh.read()


def _all(filt, data):
    return {str(u.p_index): u.text for u in filt.parse(data).units}


def _refused(filt, data, kind=ValueError, needle=""):
    try:
        filt.parse(data)
    except kind as exc:
        assert needle.lower() in str(exc).lower(), (needle, str(exc))
        return str(exc)
    raise AssertionError(f"expected {kind.__name__} for {data[:80]!r}")


def test_registered():
    exts = supported_exts()
    for ext in ("plist", "stringsdict"):
        assert ext in exts, exts
    assert get_filter("plist").fmt == "plist"
    assert get_filter("stringsdict").fmt == "stringsdict"
    print("  [registry] plist and stringsdict registered OK")


def test_identity_round_trips():
    for name, fmt in (
        ("Localizable.stringsdict", "stringsdict"),
        ("Info.plist", "plist"),
        ("Root.plist", "plist"),
    ):
        filt = get_filter(fmt)
        data = _fx(name)
        assert filt.build(data, {}) == data, name
        assert filt.build(data, _all(filt, data)) == data, name
        assert filt.build_localized(data, _all(filt, data), None) == data, name
    # CRLF, a UTF-8 BOM and UTF-16 LE with BOM survive byte for byte too
    base = _fx("Info.plist")
    crlf = base.replace(b"\n", b"\r\n")
    bom = b"\xef\xbb\xbf" + base
    u16 = b"\xff\xfe" + base.decode("utf-8").replace(
        'encoding="UTF-8"', 'encoding="UTF-16"'
    ).encode("utf-16-le")
    filt = get_filter("plist")
    for data in (crlf, bom, u16):
        assert filt.build(data, _all(filt, data)) == data
        assert [u.text for u in filt.parse(data).units] == [u.text for u in filt.parse(base).units]
    out = filt.build(u16, {"0": "Wanderhefte"})
    assert out.startswith(b"\xff\xfe"), out[:4]
    assert "<string>Wanderhefte</string>" in out[2:].decode("utf-16-le")
    out = filt.build(crlf, {"0": "Wanderhefte"})
    assert out == crlf.replace(b"Trail Notes</string>", b"Wanderhefte</string>", 1), out
    print("  [identity] unchanged builds are byte-identical (LF, CRLF, BOM, UTF-16) OK")


def test_policy_info_settings_generic():
    info = [u.text for u in get_filter("plist").parse(_fx("Info.plist")).units]
    assert info == [
        "Trail Notes",
        "Trail Notes uses the camera to attach photos to a note.",
        "Copyright 2026 Example Co. All rights reserved.",
        "Precise location draws your position on the trail map.",
        "Trail Note",
    ], info
    root = [u.text for u in get_filter("plist").parse(_fx("Root.plist")).units]
    assert root == [
        "Sync",
        "Notes sync when the app is open.",
        "Sync interval",
        "Every hour",
        "Every day",
    ], root
    generic = plistlib.dumps(
        {
            "welcome": "Welcome back",
            "button": "Save",
            "id": "com.example.thing",
            "icon": "star.fill",
            "url": "https://example.com/help",
            "build": "$(PRODUCT_NAME)",
            "version": "2.0.1",
            "slug": "home_screen",
            "camel": "isEnabled",
            "nested": [{"label": "Delete note"}],
            "count": 3,
            "flag": True,
        },
        sort_keys=False,
    )
    units = [u.text for u in get_filter("plist").parse(generic).units]
    assert units == ["Welcome back", "Save", "Delete note"], units
    print("  [policy] Info.plist, Settings.bundle and generic plists pick the right strings OK")


def test_stringsdict_units_tokens_and_splice():
    filt = get_filter("stringsdict")
    data = _fx("Localizable.stringsdict")
    units = [u.text for u in filt.parse(data).units]
    assert units == [
        "No files",
        f"{PH} file",
        f"{PH} files",
        # #693: positional %1$#@messages@ / %2$#@folders@ are free-order tokens
        "You have \u27e6p1\u27e7 in \u27e6p2\u27e7 & more",
        f"{PH} message",
        f"{PH} messages",
        "one folder",
        f"{PH} folders",
        "Tap to continue",
        "Click to continue",
        "Press <Select> to continue",
    ], units
    # %#@files@ alone and the type tags (NSStringPluralRuleType, lld) are never units
    assert not any("NSString" in u or u == "lld" for u in units)
    out = filt.build(
        data,
        {
            "1": f"{PH} Datei",
            # x tokens (a target stored before #693) still build by number
            "3": f"Du hast {PH} in \u27e6x2\u27e7 & mehr",
            "10": "Drücke <Auswahl>",
        },
    ).decode("utf-8")
    assert "<string>%lld Datei</string>" in out, out
    assert "<string>Du hast %1$#@messages@ in %2$#@folders@ &amp; mehr</string>" in out, out
    assert "<string>Drücke &lt;Auswahl&gt;</string>" in out, out
    before, after = data.decode("utf-8").splitlines(), out.splitlines()
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(changed) == 3, changed
    # a token the engine invented is dropped, never shipped as a literal
    out = filt.build(data, {"1": f"{PH} Datei \u27e6x9\u27e7"}).decode("utf-8")
    assert "<string>%lld Datei </string>" in out and "\u27e6" not in out, out
    print("  [stringsdict] units, placeholder tokens and in-place splice OK")


def test_stringsdict_plural_categories_follow_target():
    filt = get_filter("stringsdict")
    data = _fx("Localizable.stringsdict")
    tr = {k: "RU " + v for k, v in _all(filt, data).items()}
    out = filt.build_localized(data, tr, "ru")
    obj = plistlib.loads(out)
    files = obj["files_count"]["files"]
    assert list(files) == [
        "NSStringFormatSpecTypeKey",
        "NSStringFormatValueTypeKey",
        "zero",
        "one",
        "few",
        "many",
        "other",
    ], list(files)
    assert files["few"] == files["many"] == files["other"] == "RU %lld files", files
    assert list(obj["inbox_summary"]["folders"])[-3:] == ["few", "many", "other"]
    # device variants are not plural rules: untouched shape
    assert list(obj["tap_to_continue"]["NSStringDeviceSpecificRuleType"]) == [
        "iphone",
        "mac",
        "appletv",
    ]
    # Japanese has only `other`: nothing is added; build() never adds categories
    ja = plistlib.loads(filt.build_localized(data, tr, "ja"))
    assert list(ja["files_count"]["files"])[2:] == ["zero", "one", "other"]
    plain = plistlib.loads(filt.build(data, tr))
    assert "few" not in plain["files_count"]["files"]
    # binary stringsdict gets the same treatment
    binary = plistlib.dumps(plistlib.loads(data), fmt=plistlib.FMT_BINARY, sort_keys=False)
    assert filt.build(binary, _all(filt, binary)) == binary
    bobj = plistlib.loads(filt.build_localized(binary, tr, "ru"))
    assert bobj["files_count"]["files"]["many"] == "RU %lld files"
    print(
        "  [stringsdict] build_localized adds ru few/many from 'other'; ja and build() add none OK"
    )


def test_binary_plist_round_trip():
    import datetime

    src = {
        "CFBundleIdentifier": "com.example.app",
        "CFBundleDisplayName": "Trail Notes",
        "NSMicrophoneUsageDescription": "Record voice memos for a note.",
        "Blob": b"\x00\x01\x02",
        "When": datetime.datetime(2026, 10, 10, 9, 30),
        "Ratio": 1.5,
        "Enabled": False,
        "Ids": [1, 2, 3],
    }
    data = plistlib.dumps(src, fmt=plistlib.FMT_BINARY, sort_keys=False)
    filt = get_filter("plist")
    parsed = filt.parse(data)
    assert parsed.skeleton["plist_encoding"] == "binary"
    assert [u.text for u in parsed.units] == ["Trail Notes", "Record voice memos for a note."]
    assert filt.build(data, _all(filt, data)) == data
    out = filt.build(data, {"1": "Sprachnotizen aufnehmen."})
    assert out.startswith(b"bplist00"), out[:8]
    obj = plistlib.loads(out)
    assert obj["NSMicrophoneUsageDescription"] == "Sprachnotizen aufnehmen."
    for k in (
        "CFBundleIdentifier",
        "CFBundleDisplayName",
        "Blob",
        "When",
        "Ratio",
        "Enabled",
        "Ids",
    ):
        assert obj[k] == src[k], k
    assert list(obj) == list(src), "key order kept"
    print("  [binary] binary plist parsed, rebuilt as binary, other values untouched OK")


def test_records():
    recs = get_filter("stringsdict").records(_fx("Localizable.stringsdict"))
    first = recs[0]
    assert (first.key, first.value, first.unit_index) == ("files_count", "%#@files@", 0), first
    assert first.plural_forms == {"zero": "No files", "one": "%lld file", "other": "%lld files"}
    inbox = [r for r in recs if r.key == "inbox_summary"]
    assert [r.context for r in inbox] == ["", "folders"], inbox
    info = {r.key: r for r in get_filter("plist").records(_fx("Info.plist"))}
    assert info["CFBundleIdentifier"].unit_index is None
    assert info["CFBundleDisplayName"].unit_index == 0
    assert info["NSLocationTemporaryUsageDescriptionDictionary/TrailMap"].unit_index == 3
    print("  [records] key-based view for stringsdict and plist OK")


_HEAD = b'<?xml version="1.0" encoding="UTF-8"?>\n'
_DT = b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'


def test_hostile_inputs():
    filt = get_filter("plist")
    laughs = (
        _HEAD
        + b'<!DOCTYPE plist [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>\n'
        + b"<plist><dict><key>k</key><string>&b;</string></dict></plist>"
    )
    _refused(filt, laughs, ValueError, "DTD")
    xxe = (
        _HEAD
        + b'<!DOCTYPE plist [<!ENTITY x SYSTEM "file:///c:/windows/win.ini">]>\n'
        + b"<plist><dict><key>k</key><string>&x;</string></dict></plist>"
    )
    _refused(filt, xxe, ValueError, "DTD")
    undefined = (
        _HEAD + _DT + b"<plist><dict><key>k</key><string>Hi &nbsp; there</string></dict></plist>"
    )
    _refused(filt, undefined, ValueError, "entity")
    _refused(filt, _HEAD + _DT + b"<plist><dict><key>k</key><string>x</dict></plist>", ValueError)
    _refused(filt, _HEAD + b"<plist><dict><key>k</key></dict></plist>", ValueError, "no value")
    _refused(filt, _HEAD + b"<plist><dict><foo>x</foo></dict></plist>", ValueError, "unknown")
    _refused(filt, b'{ title = "Hello"; }', ValueError, "OpenStep")
    _refused(filt, b"bplist00garbage", ValueError, "binary property list")
    # a binary plist whose array contains itself (a reference cycle)
    cyc = b"bplist00" + b"\xa1\x00" + b"\x08"
    cyc += (
        b"\x00" * 6
        + b"\x01\x01"
        + (1).to_bytes(8, "big")
        + (0).to_bytes(8, "big")
        + (10).to_bytes(8, "big")
    )
    _refused(filt, cyc, InputLimitError, "nested")
    deep = (
        _HEAD
        + b"<plist>"
        + b"<array>" * 300
        + b"<string>x</string>"
        + b"</array>" * 300
        + b"</plist>"
    )
    _refused(filt, deep, InputLimitError, "nested")
    # caps: segment count, markup budget, one huge value
    old = (config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS)
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        many = _HEAD + b"<plist><array>" + b"<string>Hello there</string>" * 5 + b"</array></plist>"
        _refused(filt, many, InputLimitError, "MAX_DOCUMENT_SEGMENTS")
        budget = _HEAD + b"<plist><array>" + b"<true/>" * 200 + b"</array></plist>"
        _refused(filt, budget, InputLimitError, "MAX_DOCUMENT_SEGMENTS")
        config.MAX_DOCUMENT_SEGMENTS = old[0]
        config.MAX_SEGMENT_CHARS = 50
        big = _HEAD + b"<plist><string>" + b"word " * 40 + b"</string></plist>"
        _refused(filt, big, InputLimitError, "MAX_SEGMENT_CHARS")
    finally:
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = old
    # entities and CDATA that ARE allowed decode once and round-trip
    ok = (
        _HEAD
        + _DT
        + b"<plist><dict><key>k</key><string>Tom &amp; Jerry&#39;s "
        + b"<![CDATA[<b>show</b>]]></string></dict></plist>"
    )
    assert [u.text for u in filt.parse(ok).units] == ["Tom & Jerry's <b>show</b>"]
    assert filt.build(ok, _all(filt, ok)) == ok
    print("  [hostile] DTD/XXE/entities/OpenStep/cycles/depth/caps refused; CDATA decodes OK")


def test_web_surface_stringsdict():
    import re

    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, TargetLocale
    from tests._cli_runner import run_cli
    from tests._csrf import csrf_for

    init_db()
    with Session(engine) as s:
        p = Project(name="plist-web", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="ru"))
        s.commit()
        pid = p.id
    web = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    csrf_for(web)
    data = _fx("Localizable.stringsdict")
    r = web.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("Localizable.stringsdict", data, "application/x-plist")},
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    m = re.match(r"^/documents/(\d+)$", r.headers["location"])
    assert m, r.headers["location"]
    doc = int(m.group(1))
    code, out, err = run_cli(["doc", "translate", str(doc), "--locale", "ru"])
    assert code == 0, (code, out, err)
    r = web.get(f"/documents/{doc}/export", params={"locale": "ru"})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    obj = plistlib.loads(r.content)
    files = obj["files_count"]["files"]
    assert {"few", "many"} <= set(files), files
    assert "%lld" in files["other"] and "\u27e6" not in r.content.decode("utf-8"), files
    assert obj["files_count"]["NSStringLocalizedFormatKey"] == "%#@files@"
    print("  [web] .stringsdict upload -> translate -> ru export with few/many OK")


def main():
    test_registered()
    test_identity_round_trips()
    test_policy_info_settings_generic()
    test_stringsdict_units_tokens_and_splice()
    test_stringsdict_plural_categories_follow_target()
    test_binary_plist_round_trip()
    test_records()
    test_hostile_inputs()
    test_web_surface_stringsdict()
    print("PLIST FILTER OK")


if __name__ == "__main__":
    main()
