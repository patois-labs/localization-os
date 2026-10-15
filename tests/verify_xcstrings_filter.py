"""Apple String Catalog filter (file-format Batch 8): `.xcstrings`.

  identity   an unchanged build returns the original bytes
  units      source text per key (or the key itself), plural/device/substitution leaves,
             shouldTranslate false and stale skipped, placeholders as ⟦xN⟧ tokens
  localize   build_localized adds or replaces ONLY the target language, keeps the others,
             uses the target's CLDR plural categories, keeps argNum/formatSpecifier
  style      Xcode JSON style (" : ", 2 spaces, sorted new locale), compact, \\/ and BOM kept
  records    the key-based view (Batch 9)
  hostile    not a catalog, non-UTF-8, nesting, caps
  surfaces   API upload -> translate -> download, CLI doc upload/export

Hermetic + offline; the fixture is hand-written under tests/fixtures/formats/apple/.
Final line: "XCSTRINGS FILTER OK".
"""

import copy
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_xcstrings_")

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError

FX = os.path.join(_ROOT, "tests", "fixtures", "formats", "apple", "Localizable.xcstrings")
PH = "⟦x1⟧"
FILT = get_filter("xcstrings")


def _data() -> bytes:
    with open(FX, "rb") as fh:
        return fh.read()


def _all(data):
    return {str(u.p_index): u.text for u in FILT.parse(data).units}


def test_registered_and_identity():
    assert "xcstrings" in supported_exts()
    data = _data()
    assert FILT.build(data, {}) == data
    assert FILT.build(data, _all(data)) == data
    assert FILT.build_localized(data, {}, "ru") == data
    print("  [identity] registered; unchanged builds are byte-identical OK")


def test_units():
    units = [u.text for u in FILT.parse(_data()).units]
    assert units == [
        f"{PH} item",
        f"{PH} items",
        f"Hello, {PH}!",
        "Tap to open Settings",
        "Click to open Settings",
        f"Uploaded {PH} to the album",
        f"{PH} photo",
        f"{PH} photos",
    ], units
    # "Legacy banner" (stale) and "SKU-%@" (shouldTranslate false) are not units
    assert not any("Legacy" in u or "SKU" in u for u in units)
    print("  [units] source leaves, key-as-source, skips and tokens OK")


def test_localize_adds_only_target():
    data = _data()
    before = json.loads(data)
    tr = {k: "RU " + v for k, v in _all(data).items()}
    out = FILT.build_localized(data, tr, "ru")
    obj = json.loads(out)
    for key, entry in before["strings"].items():
        for lang, loc in entry.get("localizations", {}).items():
            assert obj["strings"][key]["localizations"][lang] == loc, (key, lang)
    items = obj["strings"]["%lld items"]["localizations"]
    assert list(items) == ["en", "fr", "ru"], list(items)
    plural = items["ru"]["variations"]["plural"]
    assert sorted(plural) == ["few", "many", "one", "other"], plural
    assert plural["one"]["stringUnit"] == {"state": "translated", "value": "RU %lld item"}
    assert plural["many"]["stringUnit"]["value"] == "RU %lld items"
    hello = obj["strings"]["Hello, %@!"]["localizations"]
    assert list(hello) == ["ru"] and hello["ru"]["stringUnit"]["value"] == "RU Hello, %@!"
    sub = obj["strings"]["upload_status"]["localizations"]["ru"]["substitutions"]["photos"]
    assert (sub["argNum"], sub["formatSpecifier"]) == (1, "lld"), sub
    assert sub["variations"]["plural"]["few"]["stringUnit"]["value"] == "RU %arg photos"
    assert "localizations" not in obj["strings"]["Legacy banner"]
    assert "localizations" not in obj["strings"]["SKU-%@"]
    # Xcode style kept: " : " separators, two-space indent, no trailing newline
    text = out.decode("utf-8")
    assert '"sourceLanguage" : "en"' in text and '\n  "strings" : {' in text
    assert not text.endswith("\n") and "⟦" not in text
    print("  [localize] ru added (one/few/many/other), en/fr/de untouched, metadata kept OK")


def test_localize_replaces_existing_and_partial():
    data = _data()
    out = json.loads(FILT.build_localized(data, {"3": "Tippen", "4": "Klicken"}, "de"))
    de = out["strings"]["Open Settings"]["localizations"]["de"]
    assert de == {
        "variations": {
            "device": {
                "iphone": {"stringUnit": {"state": "translated", "value": "Tippen"}},
                "mac": {"stringUnit": {"state": "translated", "value": "Klicken"}},
            }
        }
    }, de
    # only one plural unit translated: the other category keeps the existing French text
    fr = json.loads(FILT.build_localized(data, {"0": "%lld truc"}, "fr"))
    plural = fr["strings"]["%lld items"]["localizations"]["fr"]["variations"]["plural"]
    assert plural["one"]["stringUnit"]["value"] == "%lld truc", plural
    assert plural["other"]["stringUnit"]["value"] == "%lld éléments", plural
    # locale spelling: pt_BR matches the catalog's pt-BR convention; ja keeps only other
    ja = json.loads(FILT.build_localized(data, {"0": "%lld 件", "1": "%lld 件"}, "ja"))
    assert list(ja["strings"]["%lld items"]["localizations"]["ja"]["variations"]["plural"]) == [
        "other"
    ]
    pt = json.loads(FILT.build_localized(data, {"2": "Olá, %@!"}, "pt_BR"))
    assert "pt-BR" in pt["strings"]["Hello, %@!"]["localizations"]
    print("  [localize] existing target replaced; partial keeps old leaves; ja/pt-BR OK")


def test_build_without_locale_rewrites_source():
    data = _data()
    out = json.loads(FILT.build(data, {"2": f"Hi, {PH}!", "3": "Tap for Settings"}))
    assert out["strings"]["Hello, %@!"]["localizations"]["en"]["stringUnit"]["value"] == "Hi, %@!"
    dev = out["strings"]["Open Settings"]["localizations"]["en"]["variations"]["device"]
    assert dev["iphone"]["stringUnit"]["value"] == "Tap for Settings"
    print("  [build] no-locale build rewrites the source language in place OK")


def test_style_variants():
    compact = json.dumps(
        {
            "sourceLanguage": "en",
            "strings": {"A/B test": {"comment": "x"}, "Café": {}},
            "version": "1.0",
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).replace("/", "\\/")
    data = b"\xef\xbb\xbf" + compact.encode("utf-8") + b"\n"
    assert FILT.build(data, _all(data)) == data
    out = FILT.build_localized(data, {"0": "A/B-Test"}, "de")
    assert out.startswith(b"\xef\xbb\xbf") and out.endswith(b"}\n"), out
    text = out[3:].decode("ascii")
    assert "\n" not in text.strip() and '":' in text and '\\"' not in text
    assert "A\\/B-Test" in text and "\\u00e9" in text, text
    assert (
        json.loads(text)["strings"]["A/B test"]["localizations"]["de"]["stringUnit"]["value"]
        == "A/B-Test"
    )
    print("  [style] compact, escaped slashes, ASCII escapes, BOM and trailing newline kept OK")


def test_records():
    recs = {r.key: r for r in FILT.records(_data())}
    assert recs["%lld items"].plural_forms == {"one": "%lld item", "other": "%lld items"}
    assert recs["%lld items"].comment == "Item count in the list footer"
    assert recs["Hello, %@!"].value == "Hello, %@!" and recs["Hello, %@!"].unit_index == 2
    assert recs["SKU-%@"].unit_index is None and recs["Legacy banner"].unit_index is None
    assert recs["Open Settings"].plural_forms == {
        "iphone": "Tap to open Settings",
        "mac": "Click to open Settings",
    }
    print("  [records] key-based view OK")


def _refused(data, kind=ValueError, needle=""):
    try:
        FILT.parse(data)
    except kind as exc:
        assert needle.lower() in str(exc).lower(), (needle, str(exc))
        return
    raise AssertionError(f"expected {kind.__name__}")


def test_hostile_inputs():
    _refused(b'{"strings": []}', ValueError, "String Catalog")
    _refused(b"[1, 2]", ValueError, "String Catalog")
    _refused(b"not json", ValueError, "JSON")
    _refused('{"sourceLanguage":"en","strings":{"café":{}}}'.encode("latin-1"), ValueError, "UTF-8")
    deep = (
        b'{"sourceLanguage":"en","strings":{"k":{"localizations":{"en":'
        + b'{"variations":{"plural":{"one":' * 30
        + b"{}"
        + b"}}}" * 30
        + b"}}}}"
    )
    _refused(deep, InputLimitError, "nested")
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        many = {"sourceLanguage": "en", "strings": {f"String {i}": {} for i in range(5)}}
        _refused(json.dumps(many).encode(), InputLimitError, "MAX_DOCUMENT_SEGMENTS")
        quotes = '{"sourceLanguage":"en","strings":{},"x":[' + ",".join(['""'] * 100) + "]}"
        _refused(quotes.encode(), InputLimitError, "MAX_DOCUMENT_SEGMENTS")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print("  [hostile] non-catalog, non-UTF-8, nesting and caps refused OK")


def test_api_and_cli_surfaces():
    import re
    import tempfile

    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    init_db()
    with Session(engine) as s:
        p = Project(name="xcstrings", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="ru"))
        s.commit()
        pid = p.id
    user = security.create_user("xcs@x.test", "xcs", Role.ADMIN, "xcs-pw-123456789")
    _row, raw = apikeys.create_key(user.id, "xcs", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key
    data = _data()
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("Localizable.xcstrings", data, "application/json")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc), "--locale", "ru"])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc}/download", headers=hdr, params={"locale": "ru"})
    assert r.status_code == 200, (r.status_code, r.text)
    obj = json.loads(r.content)
    before = json.loads(data)
    for key in ("%lld items", "Open Settings"):
        for lang, loc in before["strings"][key]["localizations"].items():
            assert obj["strings"][key]["localizations"][lang] == loc, (key, lang)
    ru = obj["strings"]["%lld items"]["localizations"]["ru"]["variations"]["plural"]
    assert sorted(ru) == ["few", "many", "one", "other"], ru
    assert "%lld" in ru["one"]["stringUnit"]["value"], ru
    assert "⟦" not in r.content.decode("utf-8")
    r = client.get("/api/formats", headers=hdr)
    names = {f["ext"]: f["name"] for f in r.json()["formats"]}
    assert names.get("xcstrings") == "Apple String Catalog", names
    assert names.get("stringsdict") and names.get("plist"), names
    print("  [api] upload -> translate -> ru download keeps en/fr/de; /api/formats labels OK")

    tmp = tempfile.mkdtemp(prefix="loctms_xcs_")
    src = os.path.join(tmp, "Localizable.xcstrings")
    with open(src, "wb") as fh:
        fh.write(data)
    code, out, err = run_cli(["doc", "upload", str(pid), src])
    assert code == 0, (code, out, err)
    cli_doc = int(re.search(r"Uploaded #(\d+)", out).group(1))
    code, out, err = run_cli(["doc", "translate", str(cli_doc), "--locale", "ru"])
    assert code == 0, (code, out, err)
    dest = os.path.join(tmp, "out.xcstrings")
    code, out, err = run_cli(["doc", "export", str(cli_doc), "--locale", "ru", "-o", dest])
    assert code == 0, (code, out, err)
    with open(dest, "rb") as fh:
        exported = json.loads(fh.read())
    assert "ru" in exported["strings"]["Hello, %@!"]["localizations"]
    assert exported["strings"]["Open Settings"]["localizations"]["de"] == copy.deepcopy(
        before["strings"]["Open Settings"]["localizations"]["de"]
    )
    print("  [cli] doc upload -> translate -> export writes ru, keeps de OK")


def main():
    test_registered_and_identity()
    test_units()
    test_localize_adds_only_target()
    test_localize_replaces_existing_and_partial()
    test_build_without_locale_rewrites_source()
    test_style_variants()
    test_records()
    test_hostile_inputs()
    test_api_and_cli_surfaces()
    print("XCSTRINGS FILTER OK")


if __name__ == "__main__":
    main()
