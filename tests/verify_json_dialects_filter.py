"""JSON resource dialects (Batch 8): Flutter .arb, Chrome/WebExtension messages.json,
FormatJS / React-Intl, Transifex Structured JSON and go-i18n.

Pins, filter by filter:
  * only MESSAGE strings become units: ARB `@`/`@@` keys, `description`, `placeholders`,
    `context`, `developer_comment`, go-i18n `hash` stay source;
  * the rebuild splices into the ORIGINAL bytes: no translation = byte-identical file
    (BOM, indentation, key order, escapes kept);
  * placeholders are protected `⟦xN⟧` tokens (Chrome `$name$`/`$1`, Go `{{.X}}`, ICU `#`
    and complex arguments) and come back as the original syntax; a translation that
    drops a structural token or breaks ICU braces falls back to the source unit;
  * ICU plural/select branches are separate units; a localized build gives plurals the
    TARGET language's CLDR categories (ru one/few/many/other, ja other, ar all six,
    en ordinals one/two/few/other); `=N` branches are always kept;
  * ARB `@@locale` names the target (inserted when absent);
  * go-i18n plural objects follow the target categories; v1 array form works;
  * generic JSON keeps the old walk exactly (every string, description included),
    and an export passes the IMPORT-time dialect so a document stored before this
    change is never re-walked under a new dialect;
  * hostile inputs: deep nesting, quote budget, segment cap, non-UTF-8, non-object ARB,
    malformed and deeply nested ICU.

Fixtures are hand-written here. Hermetic + offline. Final line: "JSON DIALECTS FILTER OK".
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_jsondialect_")

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError
from app.filters.json_filter_common import detect, scan

ARB = """{
  "@@locale": "en",
  "@@last_modified": "2026-10-10T09:00:00",
  "title": "Hello {name}",
  "@title": {
    "description": "Greeting on the home page",
    "placeholders": {"name": {"type": "String", "example": "Bob"}}
  },
  "items": "{count, plural, =0{No items} one{# item} other{# items}}",
  "@items": {"description": "Shown under the cart"},
  "mixed": "You have {count, plural, one{# new message} other{# new messages}} from {who}",
  "sel": "{gender, select, male{He liked it} female{She liked it} other{They liked it}}",
  "rank": "{n, selectordinal, one{#st place} other{#th place}}",
  "quoted": "Use '{braces}' and it''s fine",
  "id": "4711"
}
"""

CHROME = """{
  "extName": {
    "message": "Tab Saver",
    "description": "Name of the extension, shown in the store"
  },
  "greet": {
    "message": "Hello $USER$, you have $1 open tabs",
    "description": "Toolbar greeting",
    "placeholders": {
      "user": {"content": "$1", "example": "Cira"}
    }
  }
}
"""

FORMATJS = """{
  "cart.count": {
    "defaultMessage": "{itemCount, plural, one{# item} other{# items}} in your cart",
    "description": "Cart badge"
  },
  "app.welcome": {
    "defaultMessage": "Welcome back, {name}!",
    "description": {"context": "home", "note": "keep it short"}
  }
}
"""

STRUCTURED = """{
  "home": {
    "title": {
      "string": "Welcome",
      "context": "page header",
      "developer_comment": "Keep it short",
      "character_limit": 20
    },
    "files": {"string": "{n, plural, one{# file} other{# files}}"}
  }
}
"""

GO = """{
  "PersonCats": {
    "description": "The number of cats a person has",
    "hash": "sha1-abc",
    "one": "{{.Name}} has {{.Count}} cat.",
    "other": "{{.Name}} has {{.Count}} cats."
  },
  "HelloPerson": "Hello {{.Name}}"
}
"""

GO_V1 = """[
  {"id": "d_days", "translation": {"one": "{{.Count}} day", "other": "{{.Count}} days"}},
  {"id": "hello", "translation": "Hello"}
]
"""

PLAIN = """{"title": "Hello", "meta": {"description": "A description", "count": "{count}"}}"""


def units(fmt, text):
    return [u.text for u in get_filter(fmt).parse(text.encode("utf-8")).units]


def all_tr(fmt, text, prefix="T:"):
    return {
        str(u.p_index): prefix + u.text for u in get_filter(fmt).parse(text.encode("utf-8")).units
    }


def test_registration():
    assert "arb" in supported_exts(), supported_exts()
    assert get_filter("arb").fmt == "arb"
    assert get_filter(".arb") is None or get_filter("arb") is not None
    print("  .arb registered as its own fmt OK")


def test_detection():
    got = {
        name: detect(scan(t))
        for name, t in [
            ("chrome", CHROME),
            ("formatjs", FORMATJS),
            ("structured", STRUCTURED),
            ("go", GO),
            ("go_v1", GO_V1),
            ("arb_shape", ARB),
            ("plain", PLAIN),
            ("flat_go", '{"a": "Hello", "b": "World"}'),
            ("list", '["a", "b"]'),
            ("mixed_chrome", '{"a": {"message": "x"}, "b": "y"}'),
            ("chrome_extra_key", '{"a": {"message": "x", "foo": "y"}}'),
            ("scalar", '"just a string"'),
        ]
    }
    assert got == {
        "chrome": "chrome",
        "formatjs": "formatjs",
        "structured": "structured",
        "go": "go",
        "go_v1": "go",
        "arb_shape": "arb",
        "plain": "plain",
        "flat_go": "plain",
        "list": "plain",
        "mixed_chrome": "plain",
        "chrome_extra_key": "plain",
        "scalar": "plain",
    }, got
    print("  dialect detection is strict (partial shapes stay plain) OK")


def test_arb_units_skip_metadata():
    got = units("arb", ARB)
    assert got == [
        "Hello {name}",
        "No items",
        "⟦x1⟧ item",
        "⟦x1⟧ items",
        "You have ⟦x1⟧ from {who}",
        "⟦x1⟧ new message",
        "⟦x1⟧ new messages",
        "He liked it",
        "She liked it",
        "They liked it",
        "⟦x1⟧st place",
        "⟦x1⟧th place",
        "Use '{braces}' and it''s fine",
        "4711",  # alphanumeric: counted like the generic walk counts it
    ], got
    joined = " ".join(got)
    for meta in ("Greeting on the home page", "Shown under the cart", "Bob", "2026-10-10"):
        assert meta not in joined, meta
    print("  ARB: messages only, @ metadata and @@ globals never units OK")


def test_identity_round_trip():
    for fmt, text in [
        ("arb", ARB),
        ("json", CHROME),
        ("json", FORMATJS),
        ("json", STRUCTURED),
        ("json", GO),
        ("json", GO_V1),
    ]:
        data = text.encode("utf-8")
        assert get_filter(fmt).build(data, {}) == data, (fmt, text[:40])
        # translations equal to the source text are byte-identical too
        same = {str(u.p_index): u.text for u in get_filter(fmt).parse(data).units}
        assert get_filter(fmt).build(data, same) == data, (fmt, "same-text")
    bom = b"\xef\xbb\xbf" + CHROME.encode("utf-8")
    assert get_filter("json").build(bom, {}) == bom
    out = get_filter("json").build(bom, {"0": "Tab Retter"})
    assert out.startswith(b"\xef\xbb\xbf") and "Tab Retter" in out.decode("utf-8-sig")
    print("  untouched files rebuild byte-identically (BOM kept) OK")


def test_arb_translated_and_locale():
    data = ARB.encode("utf-8")
    tr = all_tr("arb", ARB)
    out = get_filter("arb").build_localized(data, tr, "ru-RU").decode("utf-8")
    obj = json.loads(out)
    assert obj["@@locale"] == "ru_RU", obj["@@locale"]
    assert obj["@@last_modified"] == "2026-10-10T09:00:00"
    assert obj["@title"]["description"] == "Greeting on the home page", obj["@title"]
    assert obj["title"] == "T:Hello {name}", obj["title"]
    assert obj["items"] == (
        "{count, plural, =0{T:No items} one{T:# item} few{T:# items} many{T:# items} "
        "other{T:# items}}"
    ), obj["items"]
    assert obj["mixed"] == (
        "T:You have {count, plural, one{T:# new message} few{T:# new messages} "
        "many{T:# new messages} other{T:# new messages}} from {who}"
    ), obj["mixed"]
    assert obj["sel"] == (
        "{gender, select, male{T:He liked it} female{T:She liked it} other{T:They liked it}}"
    ), obj["sel"]
    assert obj["id"] == "T:4711"
    # ja: only `other` (=0 kept); selectordinal in ja has only `other` too.
    ja = json.loads(get_filter("arb").build_localized(data, tr, "ja"))
    assert ja["items"] == "{count, plural, =0{T:No items} other{T:# items}}", ja["items"]
    assert ja["rank"] == "{n, selectordinal, other{T:#th place}}", ja["rank"]
    assert ja["@@locale"] == "ja"
    # en ordinals: one two few other
    en = json.loads(get_filter("arb").build_localized(data, tr, "en"))
    assert en["rank"] == (
        "{n, selectordinal, one{T:#st place} two{T:#th place} few{T:#th place} other{T:#th place}}"
    ), en["rank"]
    # ar: zero one two few many other
    ar = json.loads(get_filter("arb").build_localized(data, tr, "ar"))
    assert ar["items"].count("{T:") == 7, ar["items"]  # =0 + six categories
    # plain build(): no locale switch, no category change
    plain = json.loads(get_filter("arb").build(data, tr))
    assert plain["@@locale"] == "en"
    assert plain["items"] == "{count, plural, =0{T:No items} one{T:# item} other{T:# items}}"
    print("  ARB: translated, @@locale switched, plurals follow target CLDR categories OK")


def test_arb_locale_inserted_when_absent():
    src = '{\n    "hello": "Hello",\n    "@hello": {"description": "d"}\n}\n'
    out = get_filter("arb").build_localized(src.encode(), {"0": "Hallo"}, "de-DE").decode()
    assert out == (
        '{\n    "@@locale": "de_DE",\n    "hello": "Hallo",\n'
        '    "@hello": {"description": "d"}\n}\n'
    ), out
    print("  ARB: @@locale inserted as the first entry when the source has none OK")


def test_chrome():
    got = units("json", CHROME)
    assert got == ["Tab Saver", "Hello ⟦x1⟧, you have ⟦x2⟧ open tabs"], got
    out = get_filter("json").build(
        CHROME.encode(), {"0": "Tab-Retter", "1": "Hallo ⟦x1⟧, du hast ⟦x2⟧ offene Tabs"}
    )
    obj = json.loads(out)
    assert obj["greet"]["message"] == "Hallo $USER$, du hast $1 offene Tabs", obj
    assert obj["greet"]["description"] == "Toolbar greeting"
    assert obj["greet"]["placeholders"]["user"] == {"content": "$1", "example": "Cira"}
    assert (
        out.decode()
        .replace("Tab-Retter", "Tab Saver")
        .replace("Hallo $USER$, du hast $1 offene Tabs", "Hello $USER$, you have $1 open tabs")
        == CHROME
    ), "only the two message strings changed"
    # an invented token falls back to the source message
    bad = json.loads(get_filter("json").build(CHROME.encode(), {"1": "Hallo ⟦x9⟧"}))
    assert bad["greet"]["message"] == "Hello $USER$, you have $1 open tabs", bad
    print("  Chrome messages.json: message only, $X$/$1 protected, unknown token falls back OK")


def test_formatjs():
    got = units("json", FORMATJS)
    assert got == ["⟦x1⟧ in your cart", "⟦x1⟧ item", "⟦x1⟧ items", "Welcome back, {name}!"], got
    out = json.loads(
        get_filter("json").build_localized(
            FORMATJS.encode(),
            {
                "0": "В корзине: ⟦x1⟧",
                "1": "⟦x1⟧ товар",
                "2": "⟦x1⟧ товаров",
                "3": "С возвращением, {name}!",
            },
            "ru",
        )
    )
    assert out["cart.count"]["defaultMessage"] == (
        "В корзине: {itemCount, plural, one{# товар} few{# товаров} many{# товаров} "
        "other{# товаров}}"
    ), out
    assert out["cart.count"]["description"] == "Cart badge"
    assert out["app.welcome"]["description"] == {"context": "home", "note": "keep it short"}
    # the outer unit dropped the plural token: the source outer text is kept (no lost branch)
    lost = json.loads(get_filter("json").build(FORMATJS.encode(), {"0": "In der Karre"}))
    assert lost["cart.count"]["defaultMessage"].startswith("{itemCount, plural,"), lost
    # unbalanced braces in a translation would break ICU: source kept
    brk = json.loads(get_filter("json").build(FORMATJS.encode(), {"3": "Willkommen {name"}))
    assert brk["app.welcome"]["defaultMessage"] == "Welcome back, {name}!", brk
    print("  FormatJS: defaultMessage only, ICU branches, token/brace damage falls back OK")


def test_structured():
    got = units("json", STRUCTURED)
    assert got == ["Welcome", "⟦x1⟧ file", "⟦x1⟧ files"], got
    out = json.loads(get_filter("json").build(STRUCTURED.encode(), {"0": "Bienvenue"}))
    assert out["home"]["title"] == {
        "string": "Bienvenue",
        "context": "page header",
        "developer_comment": "Keep it short",
        "character_limit": 20,
    }, out
    print("  Structured JSON: string only, context/developer_comment/limit kept OK")


def test_go():
    got = units("json", GO)
    assert got == ["⟦x1⟧ has ⟦x2⟧ cat.", "⟦x1⟧ has ⟦x2⟧ cats.", "Hello ⟦x1⟧"], got
    tr = {"0": "У ⟦x1⟧ ⟦x2⟧ кошка.", "1": "У ⟦x1⟧ ⟦x2⟧ кошек.", "2": "Привет, ⟦x1⟧"}
    out = get_filter("json").build_localized(GO.encode(), tr, "ru").decode()
    assert out == (
        "{\n"
        '  "PersonCats": {\n'
        '    "description": "The number of cats a person has",\n'
        '    "hash": "sha1-abc",\n'
        '    "one": "У {{.Name}} {{.Count}} кошка.",\n'
        '    "few": "У {{.Name}} {{.Count}} кошек.",\n'
        '    "many": "У {{.Name}} {{.Count}} кошек.",\n'
        '    "other": "У {{.Name}} {{.Count}} кошек."\n'
        "  },\n"
        '  "HelloPerson": "Привет, {{.Name}}"\n'
        "}\n"
    ), out
    ja = json.loads(get_filter("json").build_localized(GO.encode(), tr, "ja"))
    assert set(ja["PersonCats"]) == {"description", "hash", "other"}, ja
    # v1 array form
    assert units("json", GO_V1) == ["⟦x1⟧ day", "⟦x1⟧ days", "Hello"]
    v1 = json.loads(
        get_filter("json").build_localized(
            GO_V1.encode(), {"0": "⟦x1⟧ день", "1": "⟦x1⟧ дней", "2": "Привет"}, "ru"
        )
    )
    assert v1[0]["translation"] == {
        "one": "{{.Count}} день",
        "few": "{{.Count}} дней",
        "many": "{{.Count}} дней",
        "other": "{{.Count}} дней",
    }, v1
    assert v1[1] == {"id": "hello", "translation": "Привет"}
    print("  go-i18n: description/hash kept, {{.X}} protected, target plural keys OK")


def test_plain_json_unchanged():
    f = get_filter("json")
    parsed = f.parse(PLAIN.encode())
    assert parsed.skeleton == {
        "format": "json",
        "units": [
            {"p_index": 0, "text": "Hello", "style": ""},
            {"p_index": 1, "text": "A description", "style": ""},
            {"p_index": 2, "text": "{count}", "style": ""},
        ],
    }, parsed.skeleton
    out = f.build(PLAIN.encode(), {"1": "Une description"})
    # B-bytes: the translation is spliced in; the file keeps its own (compact) layout
    assert out == PLAIN.replace("A description", "Une description").encode(), out
    assert f.build_localized(PLAIN.encode(), {}, "ru") == f.build(PLAIN.encode(), {})
    # A document stored by the generic walk before this change: export passes "plain"
    # and the Chrome-shaped original is rebuilt by the OLD walk (description included).
    legacy = f.build_localized(CHROME.encode(), {"1": "Name"}, "de", dialect="plain")
    assert json.loads(legacy)["extName"]["description"] == "Name", legacy
    # a stamped dialect that no longer matches the file falls back to the generic walk
    assert f.build(CHROME.encode(), {}, dialect="go") == f.build(
        CHROME.encode(), {}, dialect="plain"
    )
    # deep generic JSON (deeper than any dialect) still parses with the old walk
    deep = ("[" * 300) + '"deep text"' + ("]" * 300)
    assert units("json", deep) == ["deep text"]
    print("  generic JSON walk unchanged; legacy documents rebuilt with the generic walk OK")


def test_i18next_plural_fill_729():
    """#729 (decision 18): i18next v4 plural keys (`_one` / `_other`) exported into Polish
    lacked `_few` / `_many`, so i18next showed the source for 2..4 and 5+. A localized
    build adds every CLDR category the target uses (filled with the translated `_other`,
    the rule app/filters/plurals.py shares with Android, Apple and ARB); ordinals use the
    ordinal categories; a lone `_other` key (`show_other`) is not a plural group."""
    f = get_filter("json")
    src = json.dumps(
        {
            "files": {
                "deleted_one": "{{count}} file was deleted",
                "deleted_other": "{{count}} files were deleted",
                "show_other": "Show other",
            },
            "place_ordinal_one": "{{count}}st",
            "place_ordinal_other": "{{count}}th",
        },
        indent=2,
    ).encode()
    tr = {"0": "S-pl", "1": "P-pl", "2": "Pokaz", "3": "O1", "4": "O-other"}
    pl = json.loads(f.build_localized(src, tr, "pl-PL", dialect="plain"))
    assert list(pl["files"]) == [
        "deleted_one",
        "deleted_few",
        "deleted_many",
        "deleted_other",
        "show_other",
    ], pl
    assert pl["files"]["deleted_few"] == pl["files"]["deleted_many"] == "P-pl", pl
    assert pl["files"]["deleted_one"] == "S-pl", pl
    ar = json.loads(f.build_localized(src, tr, "ar-SA", dialect="plain"))
    assert {k for k in ar["files"] if k.startswith("deleted_")} == {
        f"deleted_{c}" for c in ("zero", "one", "two", "few", "many", "other")
    }, ar
    # English ordinals use one/two/few/other: the target's ordinal rule, not the plural one
    en = json.loads(f.build_localized(src, tr, "en-GB", dialect="plain"))
    assert en["place_ordinal_two"] == en["place_ordinal_few"] == "O-other", en
    assert "deleted_few" not in en["files"], en
    # Japanese needs only _other: nothing added, nothing removed; build() never fills
    ja = json.loads(f.build_localized(src, tr, "ja-JP", dialect="plain"))
    assert list(ja["files"]) == ["deleted_one", "deleted_other", "show_other"], ja
    assert "deleted_few" not in json.loads(f.build(src, tr, dialect="plain"))["files"]
    print("  [#729] i18next plural keys get the target's missing categories OK")


def test_arb_suffix_plural_fill_729():
    """#729 gate (soak steward): suffix-style ARB plurals (`itemCountOne` /
    `itemCountOther`, one message per category) exported into Czech lacked `Few` / `Many`.
    A localized build adds each missing category before `...Other`, holding the translated
    `...Other`; metadata, other keys and the original layout stay byte for byte; a key the
    file already has is never touched; build() never fills."""
    f = get_filter("arb")
    src = (
        b"{\n"
        b'  "@@locale": "en",\n'
        b'  "itemCountOne": "{count} item",\n'
        b'  "itemCountOther": "{count} items",\n'
        b'  "@itemCountOther": {"placeholders": {"count": {}}},\n'
        b'  "showOther": "Show other",\n'
        b'  "dayZero": "no days",\n'
        b'  "dayFew": "OWN few",\n'
        b'  "dayOther": "{n} days"\n'
        b"}\n"
    )
    tr = {"0": "{count} polozka", "1": "{count} polozek", "2": "Jine", "3": "Z", "4": "F", "5": "D"}
    cs = f.build_localized(src, tr, "cs-CZ").decode()
    data = json.loads(cs)
    assert data["itemCountFew"] == data["itemCountMany"] == "{count} polozek", cs
    assert data["dayFew"] == "F" and data["dayMany"] == "D" and data["dayOne"] == "D", cs
    keys = list(data)
    assert keys.index("itemCountOne") < keys.index("itemCountFew") < keys.index("itemCountOther")
    assert '  "itemCountFew": "{count} polozek",\n  "itemCountMany"' in cs, cs
    assert '"@itemCountOther": {"placeholders": {"count": {}}},' in cs, cs
    assert "showOtherFew" not in cs and "showFew" not in cs, cs
    assert data["@@locale"] == "cs_CZ", cs
    ja = json.loads(f.build_localized(src, tr, "ja-JP"))
    assert "itemCountFew" not in ja and "itemCountOne" in ja, ja
    assert b"itemCountFew" not in f.build(src, tr)
    print("  [#729 gate] suffix-style ARB plurals get the target's missing categories OK")


def test_skeleton_dialect_stamp():
    sk = get_filter("json").parse(CHROME.encode()).skeleton
    assert sk["format"] == "json" and sk["dialect"] == "chrome", sk
    sk = get_filter("arb").parse(ARB.encode()).skeleton
    assert sk["format"] == "arb" and sk["dialect"] == "arb", sk
    print("  skeleton carries the import-time dialect OK")


def test_hostile_inputs():
    arb = get_filter("arb")
    for bad in (b'["not", "an", "object"]', b'"str"'):
        try:
            arb.parse(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    try:
        arb.parse(b'{"a": "caf\xe9"}')
    except ValueError as exc:
        assert "UTF-8" in str(exc)
    else:
        raise AssertionError("non-UTF-8 ARB accepted")
    try:
        arb.parse(("{" + '"a": {' * 300 + "}" * 300 + "}").encode())
    except InputLimitError:
        pass
    else:
        raise AssertionError("deep ARB accepted")
    # malformed ICU: one opaque unit, rebuilt verbatim
    src = '{"bad": "{count, plural, one{# x} other{# y}", "@bad": {}}'
    assert units("arb", src) == ["{count, plural, one{# x} other{# y}"]
    assert arb.build(src.encode(), {}) == src.encode()
    # deeply nested ICU: refused as ICU (opaque unit), never a crash
    deep = "{a, select, other{" * 60 + "x" + "}" * 60
    deep_doc = json.dumps({"d": deep})
    assert len(units("arb", deep_doc)) == 1
    # quote budget refuses before parsing
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        chrome_many = json.dumps({f"k{i}": {"message": f"word {i}"} for i in range(10)})
        arb_many = json.dumps({f"k{i}": f"word {i}" for i in range(10)})
        # the pre-parse quote budget, then the unit cap
        huge = json.dumps({f"k{i}": "" for i in range(200)})
        for fmt, doc in (("json", chrome_many), ("arb", arb_many), ("arb", huge)):
            try:
                get_filter(fmt).parse(doc.encode())
            except InputLimitError:
                pass
            else:
                raise AssertionError(fmt)
        # segment cap on units (plural branches count)
        config.MAX_DOCUMENT_SEGMENTS = 4
        branches = json.dumps(
            {"p": "{n, plural, " + " ".join(f"={i}{{w{i}}}" for i in range(6)) + "}"}
        )
        try:
            arb.parse(branches.encode())
        except InputLimitError:
            pass
        else:
            raise AssertionError("segment cap")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print("  hostile: non-object/non-UTF-8/deep ARB refused, bad ICU opaque, caps OK")


def test_export_uses_import_dialect():
    """End to end: ingest -> translate rows -> build_translated_file passes the stored
    dialect (and the ARB fmt rebuilds with the target @@locale)."""
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.models import Segment
    from app.services import content
    from app.services.export import build_translated_file
    from app.services.ingest import ingest_document

    init_db()
    with Session(engine) as s:
        p = content.project_create(s, "JsonDialects", source_locale="en-US", target_locales="ru-RU")
        doc = ingest_document(s, p, "app_en.arb", ARB.encode(), "segmented")
        assert doc.fmt == "arb", doc.fmt
        for seg in s.exec(select(Segment).where(Segment.document_id == doc.id)).all():
            seg.target_text = "T:" + seg.source_text
            seg.status = "mt"
            s.add(seg)
        s.commit()
        s.refresh(doc)
        obj = json.loads(build_translated_file(s, doc, "ru-RU"))
        assert obj["@@locale"] == "ru_RU", obj
        assert "few{T:# items}" in obj["items"], obj["items"]
        assert obj["@items"] == {"description": "Shown under the cart"}

        cdoc = ingest_document(s, p, "messages.json", CHROME.encode(), "segmented")
        assert cdoc.skeleton.get("dialect") == "chrome", cdoc.skeleton
        for seg in s.exec(select(Segment).where(Segment.document_id == cdoc.id)).all():
            seg.target_text = seg.source_text.replace("Hello", "Привет")
            seg.status = "mt"
            s.add(seg)
        s.commit()
        s.refresh(cdoc)
        out = json.loads(build_translated_file(s, cdoc, "ru-RU"))
        assert out["greet"]["message"] == "Привет $USER$, you have $1 open tabs", out
        assert out["greet"]["description"] == "Toolbar greeting"
    print("  export rebuilds with the import-time dialect (ARB + Chrome end to end) OK")


def test_gate_fixes():
    """Review gate: (1) json.loads accepts NaN/Infinity, so the dialect sniff must too (a
    generic JSON holding NaN crashed with AttributeError); (2) the splice rebuild is linear
    (re-slicing per edit took 20 s for 20000 ARB messages); (3) the OneDrive/SharePoint
    connector accepts a localized ARB whose plural branches follow the target's CLDR
    categories (3 units in, 5 out for Russian) instead of refusing it as corrupt."""
    import time

    js, arb = get_filter("json"), get_filter("arb")
    for raw in (b'{"a": NaN, "b": "hello there"}', b'[1, -Infinity, Infinity, "x y"]'):
        assert len(js.parse(raw).units) == 1, raw

    big = {"@@locale": "en"}
    for i in range(20000):
        big[f"k{i}"] = f"Message number {i} with some text"
    src = json.dumps(big, indent=2).encode()
    mapping = {str(i): "X" + u.text for i, u in enumerate(arb.parse(src).units)}
    t0 = time.monotonic()
    out = arb.build(src, mapping)
    took = time.monotonic() - t0
    assert took < 8, f"ARB rebuild of 20000 messages took {took:.1f} s"
    assert json.loads(out)["k19999"] == "XMessage number 19999 with some text"

    from app.services.connectors.msgraph import MsGraphConnector

    plural = json.dumps(
        {"@@locale": "en", "n": "{count, plural, one{# file} other{# files}}", "hi": "Hello"},
        indent=2,
    ).encode()
    units = arb.parse(plural).units
    texts = {str(i): "T" + u.text for i, u in enumerate(units)}
    conn = object.__new__(MsGraphConnector)
    built = conn.render({"filt": arb, "original": plural}, "ru", texts)
    assert json.loads(built)["@@locale"] == "ru"
    assert "few{" in json.loads(built)["n"], built
    print("  [gate] NaN JSON, linear ARB rebuild, msgraph localized plural ARB OK")


def main():
    test_gate_fixes()
    test_registration()
    test_detection()
    test_arb_units_skip_metadata()
    test_identity_round_trip()
    test_arb_translated_and_locale()
    test_arb_locale_inserted_when_absent()
    test_chrome()
    test_formatjs()
    test_structured()
    test_go()
    test_plain_json_unchanged()
    test_i18next_plural_fill_729()
    test_arb_suffix_plural_fill_729()
    test_skeleton_dialect_stamp()
    test_hostile_inputs()
    test_export_uses_import_dialect()
    print("JSON DIALECTS FILTER OK")


if __name__ == "__main__":
    main()
