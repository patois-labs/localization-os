"""Apple Xcode Localization Catalog (.xcloc) document filter (file-format Batch 16).

Spec checked: Apple "Exporting localizations" (contents.json, Localized Contents, Notes,
Source Contents; XLIFF 1.2 strings). Fixtures are hand-written and built in memory.

  layout       bundle at the archive root or under one top folder; __MACOSX/ ignored;
               several Localized Contents/*.xliff files offset into one unit list
  round trip   no translations -> the original archive byte for byte; a translation
               rewrites only that XLIFF, every other member keeps bytes, name, order,
               timestamp and compression method
  localized    build_localized stamps contents.json targetLocale (text splice, layout kept)
               and XLIFF target-language; state="new" targets written become "translated"
  placeholders %@, %1$@, %lld survive; XLIFF inline tags round-trip as tokens
  hostile      not a zip, no contents.json, two catalogs, duplicate member, contents.json not
               an object or over the cap, DOCTYPE in the XLIFF, zip bomb, segment cap

Hermetic + offline. Final line: "XCLOC FILTER OK".
"""

import base64
import io
import os
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_xcloc_")

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError
from app.services.mime import mime_for_fmt
from app.services.segment import _PLACEHOLDER_RE

XLIFF = b"""<?xml version="1.0" encoding="UTF-8"?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2">
  <file original="MyApp/Localizable.xcstrings" source-language="en" target-language="fr"
        datatype="plaintext">
    <header>
      <tool tool-id="com.apple.dt.xcode" tool-name="Xcode" tool-version="15.0" build-num="15A240d"/>
    </header>
    <body>
      <trans-unit id="Hello %@" xml:space="preserve">
        <source>Hello %@</source>
        <target state="new">Hello %@</target>
        <note>Greeting on the home screen</note>
      </trans-unit>
      <trans-unit id="%lld items" xml:space="preserve">
        <source>%1$lld items in %2$@</source>
        <target state="new">%1$lld items in %2$@</target>
      </trans-unit>
      <trans-unit id="Bold" xml:space="preserve">
        <source>Tap <g id="1">Save</g> now</source>
      </trans-unit>
      <trans-unit id="Empty" xml:space="preserve">
        <source></source>
      </trans-unit>
    </body>
  </file>
</xliff>
"""
XLIFF2 = b"""<?xml version="1.0" encoding="UTF-8"?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2">
  <file original="MyApp/InfoPlist.xcstrings" source-language="en" target-language="fr"
        datatype="plaintext">
    <body>
      <trans-unit id="CFBundleName" xml:space="preserve">
        <source>My App</source>
      </trans-unit>
    </body>
  </file>
</xliff>
"""
CONTENTS = b"""{
  "developmentRegion" : "en",
  "project" : "MyApp.xcodeproj",
  "targetLocale" : "fr",
  "toolInfo" : {
    "toolBuildNumber" : "15A240d",
    "toolID" : "com.apple.dt.xcode",
    "toolName" : "Xcode",
    "toolVersion" : "15.0"
  },
  "version" : "1.0"
}"""
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def bundle(prefix="fr.xcloc/", extra=None, xliffs=None, contents=CONTENTS, macosx=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.comment = b"made by hand"
        if prefix:
            z.writestr(zipfile.ZipInfo(prefix, (2024, 1, 2, 3, 4, 6)), b"")
        z.writestr(
            zipfile.ZipInfo(prefix + "contents.json", (2024, 1, 2, 3, 4, 6)),
            contents,
            compress_type=zipfile.ZIP_DEFLATED,
        )
        for name, body in xliffs or [("fr.xliff", XLIFF)]:
            z.writestr(
                zipfile.ZipInfo(prefix + "Localized Contents/" + name, (2024, 1, 2, 3, 4, 8)),
                body,
                compress_type=zipfile.ZIP_DEFLATED,
            )
        z.writestr(
            zipfile.ZipInfo(prefix + "Localized Contents/fr.lproj/logo.png", (2024, 1, 2, 3, 4, 8)),
            PNG_BYTES,
        )
        z.writestr(
            zipfile.ZipInfo(
                prefix + "Source Contents/MyApp/Localizable.xcstrings", (2024, 1, 2, 3, 4, 10)
            ),
            b'{"sourceLanguage":"en","strings":{"Hello %@":{}}}',
            compress_type=zipfile.ZIP_DEFLATED,
        )
        z.writestr(zipfile.ZipInfo(prefix + "Notes/screen.txt", (2024, 1, 2, 3, 4, 12)), b"note")
        if macosx:
            z.writestr("__MACOSX/fr.xcloc/._contents.json", b"\x00\x05\x16\x07junk")
        for name, body in extra or []:
            z.writestr(name, body)
    return buf.getvalue()


def members(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return [(i.filename, i.date_time, i.compress_type, z.read(i)) for i in z.infolist()]


def refused(fn, exc=ValueError, needle=""):
    try:
        fn()
    except exc as e:
        assert needle in str(e), (needle, str(e))
        return
    raise AssertionError(f"expected {exc.__name__} containing {needle!r}")


f = get_filter("xcloc")
assert f is not None and f.fmt == "xcloc" and "xcloc" in supported_exts()
assert mime_for_fmt("xcloc") == "application/zip"

# --- parse --------------------------------------------------------------------------------
SRC = bundle()
r = f.parse(SRC)
texts = [u.text for u in r.units]
assert texts[:2] == ["Hello %@", "%1$lld items in %2$@"], texts
assert len(texts) == 3, texts  # the empty <source/> is skipped (XLIFF filter rule)
assert [u.p_index for u in r.units] == [0, 1, 2]
assert _PLACEHOLDER_RE.findall(texts[2]), texts[2]  # <g> became a paired token
assert r.skeleton["bundle"] == "fr.xcloc/" and r.skeleton["target_locale"] == "fr"
assert r.skeleton["development_region"] == "en"
assert r.skeleton["xliff_files"] == ["fr.xcloc/Localized Contents/fr.xliff"]
# bundle at the archive root
r_root = f.parse(bundle(prefix="", macosx=False))
assert [u.text for u in r_root.units] == texts and r_root.skeleton["bundle"] == ""
# two XLIFF files: the second is offset by the first file's <source> count (4)
two = bundle(xliffs=[("fr.xliff", XLIFF), ("fr-extra.xliff", XLIFF2)])
r2 = f.parse(two)
assert [u.p_index for u in r2.units] == [0, 1, 2, 4] and r2.units[-1].text == "My App"
print("parse ok")

# --- round trip ---------------------------------------------------------------------------
assert f.build(SRC, {}) == SRC, "no-change build must return the original bytes"
assert f.build(SRC, {"99": "nothing there"}) == SRC
tok = texts[2]
fr_bold = tok.replace("Tap", "Touchez").replace("now", "maintenant").replace("Save", "Enregistrer")
out = f.build(SRC, {"0": "Bonjour %@", "1": "%1$lld articles dans %2$@", "2": fr_bold})
before, after = members(SRC), members(out)
assert [m[0] for m in before] == [m[0] for m in after], "member order or names changed"
for b, a in zip(before, after, strict=True):
    assert (b[1], b[2]) == (a[1], a[2]), f"{b[0]}: timestamp or compression changed"
    if not b[0].endswith("fr.xliff"):
        assert b[3] == a[3], f"{b[0]} bytes changed"
with zipfile.ZipFile(io.BytesIO(out)) as z:
    assert z.comment == b"made by hand"
    x = z.read("fr.xcloc/Localized Contents/fr.xliff").decode()
assert '<target state="translated">Bonjour %@</target>' in x, x
assert "%1$lld articles dans %2$@" in x
assert '<g id="1">Enregistrer</g>' in x and "Touchez" in x
assert "<note>Greeting on the home screen</note>" in x
assert 'target-language="fr"' in x  # plain build never touches the language
# re-parse of the output gives the same source units (sources untouched)
assert [u.text for u in f.parse(out).units] == texts
# second XLIFF reached through the offset
out2 = f.build(two, {"4": "Mon appli"})
with zipfile.ZipFile(io.BytesIO(out2)) as z:
    assert b"<target>Mon appli</target>" in z.read("fr.xcloc/Localized Contents/fr-extra.xliff")
    assert z.read("fr.xcloc/Localized Contents/fr.xliff") == XLIFF
print("round trip ok")

# --- localized build ----------------------------------------------------------------------
loc = f.build_localized(SRC, {"0": "Hallo %@"}, "de-DE")
with zipfile.ZipFile(io.BytesIO(loc)) as z:
    cj = z.read("fr.xcloc/contents.json")
    x = z.read("fr.xcloc/Localized Contents/fr.xliff").decode()
assert cj == CONTENTS.replace(b'"targetLocale" : "fr"', b'"targetLocale" : "de-DE"'), cj
assert 'target-language="de-DE"' in x and "Hallo %@" in x
# same language as the catalog: contents.json untouched
same = f.build_localized(SRC, {}, "fr")
with zipfile.ZipFile(io.BytesIO(same)) as z:
    assert z.read("fr.xcloc/contents.json") == CONTENTS
print("localized ok")

# --- hostile ------------------------------------------------------------------------------
refused(lambda: f.parse(b"not a zip at all"), ValueError, "not a zipped Xcode")
refused(lambda: f.parse(XLIFF), ValueError, "not a zipped Xcode")
no_contents = io.BytesIO()
with zipfile.ZipFile(no_contents, "w") as z:
    z.writestr("fr.xcloc/Localized Contents/fr.xliff", XLIFF)
refused(lambda: f.parse(no_contents.getvalue()), ValueError, "no contents.json")
twice = io.BytesIO()
with zipfile.ZipFile(twice, "w") as z:
    for p in ("fr.xcloc/", "de.xcloc/"):
        z.writestr(p + "contents.json", CONTENTS)
        z.writestr(p + "Localized Contents/fr.xliff", XLIFF)
refused(lambda: f.parse(twice.getvalue()), ValueError, "2 localization catalogs")
dup = io.BytesIO()
with zipfile.ZipFile(dup, "w") as z:
    z.writestr("contents.json", CONTENTS)
    z.writestr("Localized Contents/fr.xliff", XLIFF)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        z.writestr("Localized Contents/fr.xliff", XLIFF)
refused(lambda: f.parse(dup.getvalue()), ValueError, "same file twice")
refused(lambda: f.parse(bundle(contents=b"[1, 2]")), ValueError, "not a JSON object")
refused(lambda: f.parse(bundle(contents=b"{nope")), ValueError, "not valid JSON")
big = b'{"targetLocale": "fr", "pad": "' + base64.b64encode(os.urandom(800 * 1024)) + b'"}'
refused(lambda: f.parse(bundle(contents=big)), InputLimitError, "contents.json over")
evil = XLIFF.replace(
    b'<?xml version="1.0" encoding="UTF-8"?>',
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>',
)
refused(lambda: f.parse(bundle(xliffs=[("fr.xliff", evil)])), ValueError, "")
bomb = io.BytesIO()
with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("contents.json", CONTENTS)
    z.writestr("Localized Contents/fr.xliff", XLIFF)
    z.writestr("Source Contents/zeros.bin", b"\x00" * (8 * 1024 * 1024))
refused(lambda: f.parse(bomb.getvalue()), InputLimitError, "compression bomb")
units = "".join(
    f'<trans-unit id="u{i}"><source>String {i}</source></trans-unit>' for i in range(30)
)
many = XLIFF2.replace(b"<source>My App</source>", b"<source>My App</source>" + units.encode())
old_cap = config.MAX_DOCUMENT_SEGMENTS
config.MAX_DOCUMENT_SEGMENTS = 20
try:
    refused(lambda: f.parse(bundle(xliffs=[("fr.xliff", many)])), InputLimitError, "")
finally:
    config.MAX_DOCUMENT_SEGMENTS = old_cap
print("hostile ok")

print("XCLOC FILTER OK")
