"""KEYS BRIDGE (file-format Batch 9): resource filters in key-based projects.

Every document filter that exposes a `records()` key view can be imported into and exported
from a key project, discovered from the filter registry (`app/services/key_files_bridge.py`).
Round-trip identity is the test: a resource file imported as the source and exported back
with nothing changed is the same bytes.

GROUPS
  [A] DISCOVERY. The bridged set is every registered filter with records() minus the native
      JSON/PO paths; extension aliases resolve; a filter registered later lights up with no
      other edit.
  [B] THE TRIO. Android strings.xml (when the Batch 2 filter is merged) + iOS
      Localizable.strings (UTF-16 LE with BOM) + Java .properties (Latin-1, \\u escapes,
      MessageFormat apostrophes, continuation lines) imported into ONE project, each exported
      back byte-identical; a translated export re-imported is a fixed point.
  [C] TRANSLATED EXPORT. Values land in the filter's own encoding and escaping; an entry
      with no exported value keeps its template text.
  [D] PLURALS AND CONTEXTS. A Rails YAML one/other map is ONE plural key; a target-language
      form the template has no slot for is reported, never dropped silently. An INI section
      is the key's context.
  [E] COLLISIONS. A duplicate key in one file, and a key another FORMAT already defined in
      the namespace, are refused with KeyCollisionError and nothing is written.
  [F] TEMPLATE RULES. A bridged export needs a namespace and an imported source file; a key
      the template has no place for is reported on every channel (notices, headers, audit).
  [G] PLACEHOLDER-ONLY entries are not keys and survive the export untouched.
  [H] SURFACES. CLI local and --remote (through the real /api), the stdout guard for a
      non-UTF-8 file, the unknown-format refusal, the web download filename and media type,
      the git connector's format map.
  [J] NOT YET MERGED. Fixtures for resource formats whose filters are on other branches
      (stringsdict, xcstrings, plist, ARB, PHP, TOML, .desktop, .catkeys, RESX, Qt TS, WiX):
      each runs the identity round trip when its filter is registered, else prints SKIP.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). ASCII-only output.
Final line: "KEYS BRIDGE OK".
"""

import io
import json as _json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_keysbridge_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.cli import _remote
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import ProjectType, Role
from app.services import apikeys, content, keys, security, settings
from app.services import key_files as kf
from app.services import key_files_bridge as bridge

init_db()

SRC = "en-US"
_N = [0]


def _uniq() -> int:
    _N[0] += 1
    return _N[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _project(targets="es-ES"):
    with Session(engine) as s:
        p = content.project_create(
            s,
            "KB-%d" % _uniq(),  # noqa: UP031
            source_locale=SRC,
            target_locales=targets,
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _import(pid, filename, data, **kw):
    with Session(engine) as s:
        return kf.import_keys(s, pid, filename, data, **kw)


def _export(pid, locale, fmt, namespace, **kw):
    with Session(engine) as s:
        return kf.export_locale(s, pid, locale, fmt=fmt, namespace=namespace, **kw)


def _key(s, pid, name, ns):
    k = keys.get_key(s, pid, name, ns)
    if k is None:
        _fail(f"key {ns}:{name} missing")
    return k


def _set(pid, ns, name, locale, text, cat=None):
    with Session(engine) as s:
        k = _key(s, pid, name, ns)
        keys.set_value(s, k, locale, text, origin=keys.ORIGIN_HUMAN, plural_category=cat)
        s.commit()


def _key_count(pid):
    with Session(engine) as s:
        return len(keys.list_keys(s, pid))


def _err(exc_type, fn, *a, **kw) -> str:
    try:
        fn(*a, **kw)
    except exc_type as exc:
        return str(exc)
    _fail(f"expected {exc_type.__name__}, got none")


# --- hand-written fixtures --------------------------------------------------------------
PROPS = (
    "# Greeting shown on the start screen\n"
    "greeting = Hello, {0}! It''s nice to see you.\n"
    "farewell: Good\\\n"
    "    bye\n"
    "only_ph = {0}\n"
    "caf\\u00e9 = Caf\\u00e9 menu\n"
    "! bang comment\n"
    "empty=\n"
).encode("latin-1")

STRINGS = b"\xff\xfe" + (
    "/* Title on the welcome screen */\n"
    '"title" = "Welcome, %@";\n'
    "// count\n"
    '"items" = "%1$d items";\n'
    '"Done" = "Done";\n'
).encode("utf-16-le")

ANDROID = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b"<resources>\n"
    b"    <!-- App name -->\n"
    b'    <string name="app_name">Notes</string>\n'
    b'    <string name="save">Save changes</string>\n'
    b'    <string name="version" translatable="false">1.0</string>\n'
    b'    <plurals name="notes_count">\n'
    b'        <item quantity="one">%d note</item>\n'
    b'        <item quantity="other">%d notes</item>\n'
    b"    </plurals>\n"
    b"</resources>\n"
)

RAILS = (
    b"en:\n"
    b"  # cart count\n"
    b"  items:\n"
    b'    one: "%{count} item"\n'
    b'    other: "%{count} items"\n'
    b"  hello: Hello\n"
)

INI = b"[menu]\ntitle=File\n\n[dialog]\ntitle=Open file\n"


# ============================================================================
# [A] discovery
# ============================================================================
def test_a_discovery():
    got = set(bridge.bridge_formats())
    for fmt in ("ini", "properties", "resjson", "strings", "yaml"):
        assert fmt in got, (fmt, got)
    assert not ({"json", "po", "pot"} & got), got
    allf = kf.all_formats()
    assert allf[:2] == ("json", "po"), allf
    assert bridge.resolve("yml") == "yaml" and bridge.resolve(".YAML") == "yaml"
    assert bridge.resolve("pot") is None and bridge.resolve("docx") is None
    assert kf.export_ext("strings") == "strings" and kf.export_ext("po") == "po"
    assert kf.export_ext("json") == "json"
    assert kf.export_media_type("properties") == "application/octet-stream"
    print("  [A1] the bridged set is every records() filter minus json/po; aliases resolve")

    # A filter registered LATER lights up with no other edit (the point of discovery).
    from app.filters import base
    from app.filters.properties_filter import PropertiesFilter

    @base.register
    class _LateFilter(PropertiesFilter):
        fmt = "zzlate"
        exts = ("zzl",)

    try:
        assert "zzlate" in bridge.bridge_formats()
        assert bridge.resolve("zzl") == "zzlate"
        pid = _project()
        rep = _import(pid, "late.zzl", b"k = Late value\n")
        assert rep["fmt"] == "zzlate" and rep["created"] == 1, rep
        assert _export(pid, "", "zzlate", "late") == b"k = Late value\n"
    finally:
        base._FILTERS.pop("zzlate", None)
        base._ALIASES.pop("zzlate", None)
        base._ALIASES.pop("zzl", None)
    assert "zzlate" not in bridge.bridge_formats()
    print("  [A2] a filter registered later is importable and exportable with no other edit")


# ============================================================================
# [B] the trio
# ============================================================================
def test_b_trio_round_trip_identity():
    pid = _project("es-ES, ru-RU")
    files = [("Localizable.strings", STRINGS), ("messages.properties", PROPS)]
    android = bridge.resolve("xml") or bridge.resolve("android")
    if android:
        files.insert(0, ("strings.xml", ANDROID))
    else:
        print("  [B0] SKIP Android leg: no Android filter with records() is registered yet")
    for name, data in files:
        rep = _import(pid, name, data)
        assert not rep["errors"], rep
        assert rep["created"] >= 2, rep
    for name, data in files:
        ns = os.path.splitext(name)[0]
        fmt = bridge.resolve(os.path.splitext(name)[1])
        out = _export(pid, "", fmt, ns)
        if out != data:
            _fail(f"{name}: source export is not byte-identical:\n{out!r}\n{data!r}")
    print("  [B1] %d resource files in ONE project export back byte-identical" % len(files))  # noqa: UP031

    with Session(engine) as s:
        g = _key(s, pid, "greeting", "messages")
        assert keys.base_values(s, g)[""] == "Hello, {0}! It's nice to see you.", (
            "the stored value is the unit text the filter shows (apostrophes undone)"
        )
        assert g.description == "Greeting shown on the start screen", g.description
        t = _key(s, pid, "title", "Localizable")
        assert t.description == "Title on the welcome screen", t.description
        if android:
            n = _key(s, pid, "notes_count", "strings")
            assert n.is_plural, "Android <plurals> is ONE plural key"
            assert keys.get_key(s, pid, "version", "strings") is None, "translatable=false"
    print("  [B2] values are the filter's unit text; comments become descriptions")

    # Translate, export, re-import the target file: a fixed point.
    _set(pid, "messages", "greeting", "es-ES", "Hola, {0}! Es un placer.")
    _set(pid, "Localizable", "title", "es-ES", "Bienvenido, %@")
    for name, _data in files:
        ns = os.path.splitext(name)[0]
        fmt = bridge.resolve(os.path.splitext(name)[1])
        out = _export(pid, "es-ES", fmt, ns, empty_as="base")
        rep = _import(pid, name, out, locale="es-ES", namespace=ns)
        assert not rep["errors"] and rep["created"] == 0 and rep["updated"] == 0, (name, rep)
    print("  [B3] a translated export re-imported into its locale changes nothing")


# ============================================================================
# [C] translated export
# ============================================================================
def test_c_translated_export_keeps_encoding_and_escaping():
    pid = _project()
    _import(pid, "messages.properties", PROPS)
    _import(pid, "Localizable.strings", STRINGS)
    _set(pid, "messages", "greeting", "es-ES", "Hola, {0}! Que tal, señor 中.")
    _set(pid, "Localizable", "title", "es-ES", 'Bienvenido, "%@" ñ')
    props = _export(pid, "es-ES", "properties", "messages", empty_as="base")
    props.decode("latin-1")
    assert b"# Greeting shown on the start screen\n" in props, props
    assert b"\\u4e2d" in props.lower(), "non-ASCII is \\u-escaped by the .properties filter"
    assert b"farewell: Good\\\n    bye\n" in props, "an untranslated entry keeps its raw bytes"
    assert b"only_ph = {0}\n" in props
    strings = _export(pid, "es-ES", "strings", "Localizable", empty_as="base")
    assert strings.startswith(b"\xff\xfe"), "UTF-16 LE BOM kept"
    text = strings[2:].decode("utf-16-le")
    assert '"title" = "Bienvenido, \\"%@\\" ñ";' in text, text
    assert '"Done" = "Done";' in text and "/* Title on the welcome screen */" in text
    print("  [C1] translations land in the filter's encoding and escaping; the rest is untouched")


# ============================================================================
# [D] plurals and contexts
# ============================================================================
def test_d_plurals_and_contexts():
    pid = _project("ru-RU")
    rep = _import(pid, "rails.yml", RAILS)
    assert rep["created"] == 2 and not rep["errors"], rep
    with Session(engine) as s:
        k = _key(s, pid, "items", "rails")
        assert k.is_plural
        base = keys.base_values(s, k)
        assert base["one"] == "%{count} item" and base["other"] == "%{count} items", base
    assert _export(pid, "", "yaml", "rails") == RAILS
    _set(pid, "rails", "items", "ru-RU", "%{count} tovar", "one")
    _set(pid, "rails", "items", "ru-RU", "%{count} tovara", "few")
    _set(pid, "rails", "items", "ru-RU", "%{count} tovarov", "other")
    rep_list: list = []
    out = _export(pid, "ru-RU", "yaml", "rails", empty_as="base", report=rep_list).decode()
    assert out.startswith("ru-RU:") or out.startswith("ru:"), out
    assert 'one: "%{count} tovar"' in out and 'other: "%{count} tovarov"' in out, out
    missing = [r for r in rep_list if r.get("kind") == kf.NOT_IN_TEMPLATE]
    assert any(r["index"] == "items" and r["plural_category"] == "few" for r in missing), rep_list
    print("  [D1] a Rails one/other map is one plural key; a form with no slot is reported")

    pid = _project()
    rep = _import(pid, "app.ini", INI)
    assert rep["created"] == 2, rep
    with Session(engine) as s:
        ctxs = sorted(k.context for k in keys.list_keys(s, pid))
        assert ctxs == ["dialog", "menu"], ctxs
    _set_ctx(pid, "app", "title", "dialog", "es-ES", "Abrir archivo")
    out = _export(pid, "es-ES", "ini", "app", empty_as="base")
    assert out == b"[menu]\ntitle=File\n\n[dialog]\ntitle=Abrir archivo\n", out
    print("  [D2] an INI section is the key's context; same key name in two sections stays two")


def _set_ctx(pid, ns, name, ctx, locale, text):
    with Session(engine) as s:
        k = keys.get_key(s, pid, name, ns, ctx)
        keys.set_value(s, k, locale, text, origin=keys.ORIGIN_HUMAN)
        s.commit()


# ============================================================================
# [E] collisions
# ============================================================================
def test_e_collisions_are_refused():
    pid = _project()
    dup = b"a = One\nb = Two\na = Again\n"
    msg = _err(kf.KeyCollisionError, _import, pid, "dup.properties", dup)
    assert "'a'" in msg and "NOT be merged" in msg, msg
    assert _key_count(pid) == 0, "nothing written"
    print("  [E1] a duplicate key in one resource file is refused, nothing written")

    _import(pid, "Localizable.strings", STRINGS, namespace="app")
    before = _key_count(pid)
    clash = b"title = Welcome\n"
    msg = _err(kf.KeyCollisionError, _import, pid, "app.properties", clash, namespace="app")
    assert "strings" in msg and "properties" in msg, msg
    assert _key_count(pid) == before
    # the SAME format re-imported into the namespace is an ordinary merge
    rep = _import(pid, "Localizable.strings", STRINGS, namespace="app")
    assert rep["unchanged"] == before and not rep["errors"], rep
    # and a different namespace is a different key set
    rep = _import(pid, "app.properties", clash, namespace="other")
    assert rep["created"] == 1, rep
    print("  [E2] a key another format defined in the namespace is refused; never merged")


# ============================================================================
# [F] template rules
# ============================================================================
def test_f_template_rules():
    pid = _project()
    msg = _err(kf.KeyFileError, _export, pid, "", "properties", None)
    assert "one file per namespace" in msg, msg
    msg = _err(kf.KeyFileError, _export, pid, "", "properties", "messages")
    assert "import the en-US properties file" in msg, msg
    _import(pid, "messages.properties", PROPS)
    with Session(engine) as s:
        keys.create_key(s, pid, "added_by_hand", namespace="messages", base_value="Hi")
        s.commit()
    rep: list = []
    out = _export(pid, "", "properties", "messages", report=rep)
    assert out == PROPS, "a key with no place leaves the file unchanged"
    assert kf.not_in_template_keys(rep) == ["added_by_hand"], rep
    assert any("no place" in line for line in kf.export_notices(rep))
    hdr = kf.export_headers(rep)
    assert hdr.get(kf.HDR_NOT_IN_TEMPLATE) == "1", hdr
    assert kf.not_in_template_keys(kf.report_from_headers(hdr)) == ["added_by_hand"]
    assert "not_in_template=1" in kf.audit_detail(rep)
    # an explicit template wins over the stored one (the git connector's path)
    other = b"greeting = Different source\n"
    with Session(engine) as s:
        out = kf.export_locale(s, pid, "", fmt="properties", namespace="messages", template=other)
    assert out == b"greeting = Hello, {0}! It's nice to see you.\n", out
    print("  [F1] namespace + template required; unplaced keys reported on every channel")


# ============================================================================
# [G] placeholder-only entries
# ============================================================================
def test_g_placeholder_only_entries():
    pid = _project()
    rep = _import(pid, "messages.properties", PROPS)
    with Session(engine) as s:
        assert keys.get_key(s, pid, "only_ph", "messages") is None
        assert keys.get_key(s, pid, "empty", "messages") is None
    assert any("nothing to translate" in n for n in rep["notes"]), rep["notes"]
    print("  [G1] placeholder-only and empty entries are not keys, and are noted")


# ============================================================================
# [H] surfaces
# ============================================================================
def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


def _mock_transport(client):
    import httpx

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = client.request(
            request.method, path, headers=dict(request.headers), content=request.content
        )
        fwd = {"content-type": r.headers.get("content-type", "")}
        for name in (kf.HDR_NOT_IN_TEMPLATE, kf.HDR_NOT_IN_TEMPLATE_KEYS):
            if r.headers.get(name):
                fwd[name] = r.headers[name]
        if r.headers.get("content-disposition"):
            fwd["content-disposition"] = r.headers["content-disposition"]
        return httpx.Response(r.status_code, content=r.content, headers=fwd)

    return httpx.MockTransport(handler)


def test_h_cli_local_and_remote():
    settings.set_many({"key_projects_enabled": "1"})
    settings.invalidate_cache()
    pid = _project()
    tmp = tempfile.mkdtemp(prefix="kb_cli_", dir=_TMP)
    src = os.path.join(tmp, "Localizable.strings")
    with open(src, "wb") as fh:
        fh.write(STRINGS)
    rc, out, err = _run(["--json", "keys", "import", "--project", str(pid), "--file", src])
    assert rc == 0, (out, err)
    assert _json.loads(out)["fmt"] == "strings"
    dest = os.path.join(tmp, "out.strings")
    rc, out, err = _run(
        ["keys", "export", "--project", str(pid), "--format", "strings",
         "--namespace", "Localizable", "-o", dest]
    )  # fmt: skip
    assert rc == 0, (out, err)
    with open(dest, "rb") as fh:
        assert fh.read() == STRINGS
    rc, out, err = _run(
        ["keys", "export", "--project", str(pid), "--format", "strings",
         "--namespace", "Localizable"]
    )  # fmt: skip
    assert rc != 0 and "-o/--out" in err, (rc, out, err)
    rc, out, err = _run(
        ["keys", "import", "--project", str(pid), "--file", src, "--format", "nosuchfmt"]
    )
    assert rc != 0 and "properties" in err and "strings" in err, err
    print("  [H1] CLI local import/export byte-identical; stdout and unknown format refused")

    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        user = security.create_user(
            "kb-remote@x.com", "kb-remote@x.com", Role.ADMIN, "kb-pw-1234567890"
        )
        _row, tok = apikeys.create_key(user.id, "kb", scopes="api keys")
        client = TestClient(app)
        _remote._set_transport(_mock_transport(client))
        try:
            pid2 = _project()
            psrc = os.path.join(tmp, "messages.properties")
            with open(psrc, "wb") as fh:
                fh.write(PROPS)
            base = ["--remote", "http://127.0.0.1", "--token", tok]
            rc, out, err = _run(
                [*base, "--json", "keys", "import", "--project", str(pid2), "--file", psrc]
            )
            assert rc == 0 and _json.loads(out)["created"] == 3, (out, err)
            rdest = os.path.join(tmp, "remote.properties")
            rc, out, err = _run(
                [*base, "keys", "export", "--project", str(pid2), "--format", "properties",
                 "--namespace", "messages", "-o", rdest]
            )  # fmt: skip
            assert rc == 0, (out, err)
            with open(rdest, "rb") as fh:
                assert fh.read() == PROPS, "the --remote export is byte-identical too"
            r = client.get(
                "/api/projects/%d/keys/export?fmt=properties&namespace=messages" % pid2,  # noqa: UP031
                headers={"X-Loc-Token": tok},
            )
            assert r.status_code == 200 and r.content == PROPS, r.status_code
            assert r.headers["content-type"].startswith("application/octet-stream"), r.headers
            assert ".properties" in r.headers.get("content-disposition", ""), r.headers
        finally:
            _remote._set_transport(None)
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  [H2] --remote import/export through /api is byte-identical; download is .properties")


def test_h_git_connector_formats():
    from app.services.connectors import git_core

    assert git_core._key_format(".json") == "json" and git_core._key_format(".po") == "po"
    assert git_core._key_format(".properties") == "properties"
    assert git_core._key_format(".yaml") == "yaml" and git_core._key_format(".yml") == "yaml"
    assert git_core._key_format(".docx") is None and git_core._key_format(".pot") is None
    st = git_core.load_settings(
        {"repo": "acme/site", "source_paths": "res/{locale}/messages.properties"}, 1
    )
    plan = git_core.plan_pairs(st, ["es-ES"])
    assert plan.pairs and plan.pairs[0].fmt == "properties", plan.pairs
    print("  [H3] the git connector accepts every bridged extension and refuses the rest")


# ============================================================================
# [J] formats whose filters are not merged yet (skip when absent)
# ============================================================================
PENDING = [
    (
        "stringsdict",
        "Localizable.stringsdict",
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        b'<plist version="1.0">\n<dict>\n'
        b"\t<key>files</key>\n\t<dict>\n"
        b"\t\t<key>NSStringLocalizedFormatKey</key>\n\t\t<string>%#@n@</string>\n"
        b"\t\t<key>n</key>\n\t\t<dict>\n"
        b"\t\t\t<key>NSStringFormatSpecTypeKey</key>\n"
        b"\t\t\t<string>NSStringPluralRuleType</string>\n"
        b"\t\t\t<key>NSStringFormatValueTypeKey</key>\n\t\t\t<string>d</string>\n"
        b"\t\t\t<key>one</key>\n\t\t\t<string>%d file</string>\n"
        b"\t\t\t<key>other</key>\n\t\t\t<string>%d files</string>\n"
        b"\t\t</dict>\n\t</dict>\n</dict>\n</plist>\n",
    ),
    (
        "xcstrings",
        "Localizable.xcstrings",
        b'{\n  "sourceLanguage" : "en",\n  "strings" : {\n    "Hello" : {\n'
        b'      "comment" : "Greeting",\n      "localizations" : {\n        "en" : {\n'
        b'          "stringUnit" : {\n            "state" : "translated",\n'
        b'            "value" : "Hello there"\n          }\n        }\n      }\n    }\n  },\n'
        b'  "version" : "1.0"\n}\n',
    ),
    (
        "arb",
        "app_en.arb",
        b'{\n  "@@locale": "en",\n  "hello": "Hello {name}",\n'
        b'  "@hello": {"description": "Greeting"},\n  "bye": "Goodbye"\n}\n',
    ),
    (
        "php",
        "messages.php",
        b"<?php\nreturn [\n    'hello' => 'Hello',\n    'bye' => \"Goodbye\",\n];\n",
    ),
    ("toml", "messages.toml", b'# greeting\nhello = "Hello"\n\n[menu]\nopen = "Open file"\n'),
    (
        "desktop",
        "app.desktop",
        b"[Desktop Entry]\nType=Application\nName=Notes\nComment=Take notes\nExec=notes\n",
    ),
    ("catkeys", "en.catkeys", b"1\tenglish\tx-vnd.App\t12345\nOpen\tMenu\t\tOpen\n"),
    (
        "resx",
        "Resources.resx",
        b'<?xml version="1.0" encoding="utf-8"?>\n<root>\n'
        b'  <data name="Hello" xml:space="preserve">\n    <value>Hello</value>\n'
        b"    <comment>Greeting</comment>\n  </data>\n</root>\n",
    ),
    (
        "ts",
        "app_en.ts",
        b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n'
        b'<TS version="2.1" language="en_US">\n<context>\n    <name>Main</name>\n'
        b"    <message>\n        <source>Open</source>\n"
        b'        <translation type="unfinished"></translation>\n    </message>\n'
        b"</context>\n</TS>\n",
    ),
    (
        "wxl",
        "en-us.wxl",
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<WixLocalization Culture="en-us" xmlns="http://wixtoolset.org/schemas/v4/wxl">\n'
        b'  <String Id="Welcome" Value="Welcome to setup" />\n</WixLocalization>\n',
    ),
]


def test_j_pending_formats():
    ran = 0
    for fmt, filename, data in PENDING:
        canon = bridge.resolve(fmt) or bridge.resolve(os.path.splitext(filename)[1])
        if not canon:
            print(f"  [J] SKIP {fmt}: no filter with records() registered yet")
            continue
        pid = _project()
        ns = os.path.splitext(filename)[0].replace(".", "_")
        rep = _import(pid, filename, data, fmt=canon, namespace=ns)
        assert not rep["errors"], (fmt, rep)
        out = _export(pid, "", canon, ns)
        if out != data:
            _fail(f"{fmt}: source export is not byte-identical:\n{out!r}\n{data!r}")
        ran += 1
        print(f"  [J] {fmt}: imported ({rep['created']} keys) and exported byte-identical")
    print("  [J] %d pending format(s) exercised, %d skipped" % (ran, len(PENDING) - ran))  # noqa: UP031


# ============================================================================
# [K] gate (train-13): template lifecycle + Apple plural_units
# ============================================================================
def test_k_template_lifecycle():
    from types import SimpleNamespace

    from app.models import Project
    from app.services import content_projects

    pid = _project()
    rep = _import(pid, "messages.properties", PROPS)
    assert rep["created"] >= 1 and not rep["errors"], rep
    with Session(engine) as s:
        proj = s.get(Project, pid)
        ghost = SimpleNamespace(id=proj.id, created_at=proj.created_at)
        assert bridge.load_template(proj, "messages", "properties") == PROPS
        # a project that later takes this rowid (SQLite reuses ids) never sees the template
        reborn = SimpleNamespace(id=proj.id, created_at=None)
        assert bridge.template_key(reborn, "messages", "properties") != bridge.template_key(
            proj, "messages", "properties"
        )
    with Session(engine) as s:
        content_projects.delete_project(s, pid)
    if bridge.load_template(ghost, "messages", "properties") is not None:
        _fail("project delete left the keys-bridge template blob behind")
    print("  [K1] template keyed by project birth; project delete removes the template blob")

    # an import with no keys stores no template (delete finds templates via key namespaces)
    pid = _project()
    rep = _import(pid, "empty.properties", b"# nothing here\n")
    with Session(engine) as s:
        assert bridge.load_template(s.get(Project, pid), "empty", "properties") is None
    print("  [K2] a key-less base import stores no template")


def test_k_apple_plurals():
    canon = bridge.resolve("stringsdict")
    if not canon:
        print("  [K3] SKIP stringsdict: filter not registered")
        return
    data = {f: d for f, _n, d in PENDING}["stringsdict"]
    pid = _project()
    rep = _import(pid, "Localizable.stringsdict", data, fmt=canon, namespace="loc")
    assert rep["created"] == 1 and not rep["errors"], rep
    with Session(engine) as s:
        k = _key(s, pid, "files", "loc")
        assert k.is_plural
        base = keys.base_values(s, k)
        # real placeholders in the key value, never the pipeline's internal tokens
        assert (base["one"], base["other"]) == ("%d file", "%d files"), base
    _set(pid, "loc", "files", "es-ES", "%d archivo", "one")
    _set(pid, "loc", "files", "es-ES", "%d archivos", "other")
    out = _export(pid, "es-ES", canon, "loc", empty_as="base")
    assert b"<string>%d archivo</string>" in out and b"<string>%d archivos</string>" in out, out
    print("  [K3] stringsdict plural forms import as one plural key and export per form")
    xc = (
        b'{\n  "sourceLanguage" : "en",\n  "strings" : {\n    "%lld songs" : {\n'
        b'      "localizations" : {\n        "en" : {\n          "variations" : {\n'
        b'            "plural" : {\n'
        b'      "one" : {"stringUnit" : {"state" : "translated", "value" : "%lld song"}},\n'
        b'      "other" : {"stringUnit" : {"state" : "translated", "value" : "%lld songs"}}\n'
        b"            }\n          }\n        }\n      }\n    }\n  },\n"
        b'  "version" : "1.0"\n}\n'
    )
    if bridge.resolve("xcstrings"):
        pid = _project()
        rep = _import(pid, "Localizable.xcstrings", xc, fmt="xcstrings", namespace="cat")
        assert rep["created"] == 1 and not rep["errors"], rep
        with Session(engine) as s:
            base = keys.base_values(s, _key(s, pid, "%lld songs", "cat"))
            assert (base["one"], base["other"]) == ("%lld song", "%lld songs"), base
        print("  [K4] xcstrings plural variation imports as one plural key")
    if bridge.resolve("toml"):
        src = b'hello = "Hi {{.Name}}, you have %d"\n'
        pid = _project()
        rep = _import(pid, "active.toml", src, fmt="toml", namespace="go")
        assert rep["created"] == 1 and not rep["errors"], rep
        with Session(engine) as s:
            base = keys.base_values(s, _key(s, pid, "hello", "go"))
            assert base[""] == "Hi {{.Name}}, you have %d", base
        assert _export(pid, "", "toml", "go") == src
        _set(pid, "go", "hello", "es-ES", "Hola {{.Name}}, tienes %d")
        out = _export(pid, "es-ES", "toml", "go", empty_as="base")
        assert out == b'hello = "Hola {{.Name}}, tienes %d"\n', out
        print("  [K5] TOML template actions are real text in the key and come back on export")


def main():
    test_a_discovery()
    test_b_trio_round_trip_identity()
    test_c_translated_export_keeps_encoding_and_escaping()
    test_d_plurals_and_contexts()
    test_e_collisions_are_refused()
    test_f_template_rules()
    test_g_placeholder_only_entries()
    test_h_cli_local_and_remote()
    test_h_git_connector_formats()
    test_j_pending_formats()
    test_k_template_lifecycle()
    test_k_apple_plurals()
    print("KEYS BRIDGE OK")


if __name__ == "__main__":
    main()
