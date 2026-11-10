"""Bilingua adapter (D27).

  default   with LOC_TMS_BILINGUA_FORMATS unset, get_filter returns the Python filter for every
            format; with it set, only an explicit new_import=True gets the adapter: a call with
            no skeleton, a None or {} skeleton or a Python skeleton keeps the Python filter.
  formats   waves 1 to 4 of the plan are routed, each to a Bilingua format id the wheel has.
  oracle    the Python token oracle (positional rule, look-alike numbers skipped, unknown and
            wrong-kind tokens kept as text, #814) is pure and reversible; with the wheel it
            gives exactly what bilingua.to_locos_text / from_locos_text give (BILI-37).
  errors    every BilinguaError code maps to its LocOS refusal (plan 5.6), a Rust panic too.
  ids       units, p_index, style and token text come from bilingua.locos_units and equal the
            Python filter's for md, json and po; a build maps p_index back to segment ids.
  tokens    docx/pptx/xlsx pairs are `r` tokens, html pairs by id, look-alike numbers skipped;
            an unknown token in a translation stays in the file as typed (#814).
  pinning   the stamp comes from doc["parser"] (binary skeletons too: md, po); profile name,
            profile version and schema must match; the crate and wheel versions are
            informational; units that moved, a changed original or other ids are refused.
  encoding  raw bytes and the source language go to Bilingua (no Python pre-decode):
            source_encoding, text_code_page (decision 22) and the JSON strict UTF-8 refusal.
  empty     zero units is EmptyDocumentError.
  wave 2-4  the Python filters' decode, budget and package gates, refusal sentences and the
            skeleton keys they record, each against the Python filter on the same bytes; wave 4
            also the comment option, XLSX column mode, DOCX warnings and xml_target_text.
  limits    LocOS's upload, segment, line and zip budgets apply before Bilingua is called.
  options   export keywords become per-format options; defaults are not options.
  export    an unavailable module or a parser mismatch is a clean ExportUnreadableBlob.
  fallback  with the bilingua module missing (or too old) the switch is ignored and a
            Bilingua-made document says plainly that it needs the module.

The wheel-dependent parts run only when the bilingua module imports (the pure parts always
run, on Windows too). Hermetic + offline. Final line: "BILINGUA ADAPTER OK".
"""

import copy
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_bilingua_adapter_")

from app.filters import bilingua_adapter as ba
from app.filters import get_filter
from app.filters.base import EmptyDocumentError, Filter, InputLimitError, UnsupportedEncodingError

ENV = ba.ENV_VAR
FORMATS = list(ba._FORMATS)
WAVES_1_2 = [
    "md",
    "json",
    "po",
    "txt",
    "srt",
    "strings",
    "properties",
    "ftl",
    "dtd",
    "resx",
    "wxl",
    "xaml",
    "json5",
    "arb",
    "resjson",
    "toml",
    "ini",
    "yaml",
    "xcstrings",
    "xcloc",
    "js",
]
WAVE_3 = ["xliff", "sdlxliff", "mqxliff", "mxliff", "txlf", "txml", "ttx", "xlz", "mqxlz", "sdlrpx"]
WAVE_4 = ["docx", "xlsx", "pptx", "odt", "ods", "odp", "odg", "vsdx", "idml", "icml", "mif"]
WAVE_5 = ["html", "epub", "eml", "flprjzip", "aspx", "jsp", "erb", "php", "phtml", "shtml", "mdx"]
WAVE_5 += ["hhc", "hhk", "hhp", "xml", "dita", "docbook", "xslt", "xmb", "xtb", "ttml"]
OP, CL = "⟦", "⟧"


def tag(i, kind, display, free=False):
    t = {"display": display, "id": i, "kind": kind}
    if free:
        t["free"] = True
    return {"tag": t}


def txt(s):
    return {"text": s}


CASES = [
    [txt("plain")],
    [txt("a "), tag("1", "open", "<b>"), txt("bold"), tag("1", "close", "</b>"), txt(" z")],
    [  # nested pairs, a point code, a free placeholder
        tag("1", "open", "<b>"),
        tag("2", "open", "<i>"),
        txt("x"),
        tag("2", "close", "</i>"),
        tag("1", "close", "</b>"),
        tag("3", "standalone", "<br/>"),
        tag("4", "standalone", "{name}", free=True),
    ],
    [txt("a"), tag("7", "open", "<u>"), txt("never closed")],  # unpaired open
    [tag("8", "close", "</s>"), txt("close only")],  # unpaired close
    [  # crossed pairs: the inner side becomes point codes, nothing is lost
        tag("1", "open", "<a>"),
        tag("2", "open", "<b>"),
        tag("1", "close", "</a>"),
        tag("2", "close", "</b>"),
    ],
    [txt(f"lit {OP}x1{CL} "), tag("5", "standalone", "<br/>")],  # look-alike: number 1 skipped
    [txt(f"lit {OP}g2{CL} "), tag("5", "open", "<b>"), txt("b"), tag("5", "close", "</b>")],
]
DAMAGE = [
    f"{OP}zz",
    f"stray {CL}",
    f"a{OP}g9{CL}b{OP}/g9{CL}c",
    f"{OP}/x1{CL}",
    f"{OP}g1{CL}a{OP}/g1{CL} {OP}g1{CL}b{OP}/g1{CL}",
    f"x {OP}g1{CL}open to the end",
    f"no open{OP}/g1{CL} here",
    f"{OP}x1{CL} kind",
    f"{OP}r1{CL}other letter{OP}/r1{CL}",
    f"{OP}1{CL}no letter{OP}/1{CL}",
    f"{OP}p1{CL} {OP}x3{CL}{OP}3{CL}{OP}g3{CL}{OP}/x3{CL}{OP}p4{CL}{OP}x4{CL}",
]


def test_default_off():
    os.environ.pop(ENV, None)
    for fmt in FORMATS:
        f = get_filter(fmt)
        assert f is not None and not isinstance(f, ba.BilinguaFilter), fmt
    assert get_filter("md").fmt == "md"
    # a skeleton that came from the Python filter keeps it, switch or no switch
    os.environ[ENV] = ",".join(FORMATS)
    try:
        py_skel = {"format": "md", "units": [{"p_index": 0, "text": "x", "style": ""}]}
        f = get_filter("md", skeleton=py_skel)
        assert not isinstance(f, ba.BilinguaFilter)
        f = get_filter("md", skeleton=py_skel, new_import=True)
        assert not isinstance(f, ba.BilinguaFilter)  # an existing Python document stays Python
        # no skeleton, None, {}: every existing-document call site keeps the Python filter;
        # only the explicit first parse gets the adapter
        for skel in (None, {}):
            assert not isinstance(get_filter("md", skeleton=skel), ba.BilinguaFilter)
        assert not isinstance(get_filter("md"), ba.BilinguaFilter)
        if ba.available():
            assert isinstance(get_filter("md", new_import=True), ba.BilinguaFilter)
            assert isinstance(get_filter("vtt", new_import=True), ba.BilinguaFilter)  # alias
        # a format that is not listed is untouched
        os.environ[ENV] = "json"
        assert not isinstance(get_filter("md", new_import=True), ba.BilinguaFilter)
        # an unknown format name in the list does nothing
        os.environ[ENV] = "nope, ,"
        assert not isinstance(get_filter("md", new_import=True), ba.BilinguaFilter)
    finally:
        os.environ.pop(ENV, None)
    assert ba.enabled_formats() == set()
    os.environ[ENV] = " MD , json "
    try:
        assert ba.enabled_formats() == {"md", "json"}
    finally:
        os.environ.pop(ENV, None)
    # T5: the stored `bilingua_formats` setting is the switch (the env var overrides it,
    # set-but-empty forces off), so select() and get_filter agree without a fallback.
    from app.db import init_db
    from app.services import settings

    init_db()
    settings.set_many({"bilingua_formats": "md,po"})
    try:
        assert ba.enabled_formats() == {"md", "po"}, ba.enabled_formats()
        os.environ[ENV] = ""
        assert ba.enabled_formats() == set()
        os.environ[ENV] = "json"
        assert ba.enabled_formats() == {"json"}
        os.environ.pop(ENV)
        if ba.available():
            assert isinstance(ba.select("md", None, new_import=True), ba.BilinguaFilter)
            assert ba.select("json", None, new_import=True) is None
        assert ba.select("md", None) is None  # not a new import: Python
    finally:
        os.environ.pop(ENV, None)
        settings.set_many({"bilingua_formats": ""})
    assert ba.enabled_formats() == set()
    # a settings fault leaves everything on Python
    real_get = settings.bilingua_formats
    settings.bilingua_formats = lambda: 1 / 0
    try:
        assert ba.enabled_formats() == set()
    finally:
        settings.bilingua_formats = real_get
    # Bilingua encoding labels become LocOS codec names; only a decision 22 code page other
    # than Windows-1252 is a text_code_page stamp
    for label, codec, stamp in (
        ("windows-1252", "cp1252", None),
        ("windows-1251", "cp1251", "cp1251"),
        ("Shift_JIS", "cp932", "cp932"),
        ("EUC-KR", "cp949", "cp949"),
        ("Big5", "cp950", "cp950"),
        ("GBK", "gbk", "gbk"),
        ("ISO-8859-8", "iso8859-8", None),
        ("UTF-8", "utf-8", None),
        ("UTF-16LE", "utf-16-le", None),
    ):
        assert ba._codec(label) == codec, (label, ba._codec(label))
        assert ba._hint_code_page(ba._codec(label)) == stamp, label
    assert ba._codec(None) is None and ba._codec("") is None
    assert isinstance(get_filter("md"), Filter)


def test_formats():
    missing = [f for f in WAVES_1_2 if f not in ba._FORMATS]
    assert not missing, f"waves 1 and 2 not routed: {missing}"
    for fmt in WAVES_1_2:
        py = get_filter(fmt, _python=True)
        assert py is not None and py.fmt == fmt, (fmt, py)
    assert ba._PAIR_LETTER["xlsx"] == "r" and ba._PAIR_LETTER["docx"] == "r"
    # wave 3 (bilingual and CAT); a Trados package document is LocOS's sdlrpx, Bilingua's sdlppx
    for fmt in WAVE_3:
        assert fmt in ba._FORMATS and fmt in ba._DECODE_GATES, fmt
        py = get_filter(fmt, _python=True)
        assert py is not None and py.fmt == fmt, (fmt, py)
    assert ba._FORMATS["sdlrpx"][0] == ("sdlppx",)
    # wave 4 (Office and DTP): each routed with its gate and its Python refusal path
    for fmt in WAVE_4:
        assert fmt in ba._FORMATS and fmt in ba._DECODE_GATES, fmt
        assert fmt in ba._REFUSAL_BY_PYTHON, fmt
        py = get_filter(fmt, _python=True)
        assert py is not None and py.fmt == fmt, (fmt, py)
    # wave 5 (HTML and XML family); svg stays Python (LocOS reads it as `image`)
    for fmt in WAVE_5:
        assert fmt in ba._FORMATS, fmt
        py = get_filter(fmt, _python=True)
        assert py is not None and py.fmt == fmt, (fmt, py)
    assert "svg" not in ba._FORMATS and get_filter("svg", _python=True).fmt == "image"


def test_oracle():
    for inl in CASES:
        text, cmap = ba.inlines_to_locos(inl)
        back, problems = ba.locos_to_inlines(text, cmap)
        assert back == inl, (inl, text, back)
        # only a look-alike in the source is listed (it stays text)
        assert all(p["kind"] == "unknown" for p in problems), problems
    text, cmap = ba.inlines_to_locos(CASES[2])
    assert text == f"{OP}g1{CL}{OP}g2{CL}x{OP}/g2{CL}{OP}/g1{CL}{OP}x3{CL}{OP}p4{CL}", text
    assert cmap["1"]["open"] == "<b>" and cmap["1"]["close"] == "</b>"
    text, cmap = ba.inlines_to_locos(CASES[6])
    assert text == f"lit {OP}x1{CL} {OP}x2{CL}" and list(cmap) == ["2"], (text, cmap)
    text, _m = ba.inlines_to_locos(CASES[1], "r")
    assert text == f"a {OP}r1{CL}bold{OP}/r1{CL} z", text
    # reordered tokens: the tags come back where the translator put them
    text, cmap = ba.inlines_to_locos(CASES[1])
    inl, problems = ba.locos_to_inlines(f"{OP}g1{CL}gras{OP}/g1{CL} a", cmap)
    assert not problems and inl[0] == tag("1", "open", "<b>") and inl[-1] == txt(" a"), inl
    # p and x are interchangeable on read (a model may normalise the letter)
    _t, fmap = ba.inlines_to_locos([tag("4", "standalone", "{n}", free=True)])
    inl, problems = ba.locos_to_inlines(f"{OP}x1{CL}", fmap)
    assert not problems and inl == [tag("4", "standalone", "{n}", free=True)], inl
    # #814: unknown, malformed and wrong-kind tokens stay in the text as typed, and are listed
    for bad, left in (
        (f"{OP}zz", f"{OP}zz"),
        (f"stray {CL}", f"stray {CL}"),
        (f"a{OP}g9{CL}b{OP}/g9{CL}c", f"a{OP}g9{CL}b{OP}/g9{CL}c"),
        (f"{OP}/x1{CL}", f"{OP}/x1{CL}"),
    ):
        inl, problems = ba.locos_to_inlines(bad, fmap)
        assert problems, bad
        assert "".join(i.get("text", "") for i in inl) == left, (bad, inl)
    inl, problems = ba.locos_to_inlines(f"use {OP}x1{CL} as is", {})
    assert inl == [txt(f"use {OP}x1{CL} as is")] and problems[0]["kind"] == "unknown", inl
    assert ba.source_literals([txt(f"use {OP}x1{CL} as is"), tag("1", "standalone", "<br/>")]) == {
        f"{OP}x1{CL}"
    }


class FakeError(Exception):
    def __init__(self, code, msg="boom"):
        super().__init__(msg)
        self.code = code


def test_error_table():
    want = {
        "limit": InputLimitError,
        "parse": ba.BilinguaParseError,
        "unsupported": ba.BilinguaParseError,
        "encoding": ba.BilinguaParseError,
        "invalid_argument": ba.BilinguaParseError,
        "io": ba.BilinguaParseError,
        "error": ba.BilinguaParseError,
        "parser_version": ba.BilinguaParserMismatch,
        "invalid_document": ba.BilinguaParseError,
        "write": ba.BilinguaParseError,
        "unwritable": ba.BilinguaWriteRefused,
        "locked": ba.BilinguaWriteRefused,
        "invented_tag": ba.BilinguaWriteRefused,
        "duplicated_tag": ba.BilinguaWriteRefused,
        "unknown_segment": ba.BilinguaWriteRefused,
        "internal": ba.BilinguaParseError,
    }
    for code, cls in want.items():
        exc = ba._fail(FakeError(code, f"segment u3: {code} happened"), "md")
        assert type(exc) is cls, (code, type(exc))
        # every refusal is a ValueError (the doors already map those); none is a 500
        assert isinstance(exc, ValueError), code
        if cls is not InputLimitError:
            assert exc.code == code, (code, exc.code)
    # unwritable is NOT an unreadable blob: export treats it as the filters' ValueError path
    assert not isinstance(ba._fail(FakeError("unwritable")), ba.BilinguaParseError)
    for code in ("parser_version", "invalid_document", "write"):
        assert "Re-import" in str(ba._fail(FakeError(code))), code
    assert "logged" in str(ba._fail(FakeError("internal")))
    assert "boom" not in str(ba._fail(FakeError("internal"))), "a panic text is not shown"
    lim = ba._fail(FakeError("limit", "This file has more than 3 segments. Split it."))
    assert "MAX_DOCUMENT_SEGMENTS" in str(lim), lim
    # W2: the pre-parse budget sentences of the wave-2 crates name the setting too
    for text in (
        "This String Catalog has far more strings than the limit of 3 segments allows. Split it.",
        "This DTD file has far more declarations than 50000 segments allow. Split it.",
        "This InCopy file has far more markup than the 50000 unit limit allows. Split it.",
    ):
        lim = str(ba._fail(FakeError("limit", text)))
        assert lim.count("MAX_DOCUMENT_SEGMENTS") == 1 and "Split it." in lim, lim
    # W2c: the class comes from the code, not from words in the message. A nesting cap is
    # code `limit` in every crate since the W3 wheel (json, docx, pptx, xlsx said `parse`):
    # a `parse` refusal is never a limit, whatever its words
    assert not hasattr(ba, "_DEPTH_AS_PARSE"), "W4c: every nesting cap is code limit"
    deep = FakeError("parse", "not a json file: nesting is too deep (line 1, column 300)")
    deep.detail = "nesting is too deep (line 1, column 300)"
    for fmt in ("json", "docx", "md"):
        assert type(ba._fail(deep, fmt)) is ba.BilinguaParseError, fmt
    capped = FakeError("limit", "word/document.xml: elements nested deeper than 200")
    assert isinstance(ba._fail(capped, "docx"), InputLimitError)
    words = FakeError("parse", "not a po file: msgid nests too deep in a comment")
    assert type(ba._fail(words, "po")) is ba.BilinguaParseError, "words alone are not a limit"
    lim = FakeError("limit", "This YAML file is nested too deeply.")
    assert str(ba._fail(lim, "yaml")) == "This YAML file is nested too deeply."
    # .detail rides along on the adapter's own error (the js and encoding mappings read it)
    exc = ba._fail(FakeError("parse", "not a toml file: bad key"), "toml")
    assert exc.detail == "not a toml file: bad key", "no .detail on the engine error: message"
    bare = FakeError("parse", "not a toml file: bad key")
    bare.detail = "bad key"
    assert ba._fail(bare, "toml").detail == "bad key"
    js = ba.BilinguaParseError(
        "Bilingua could not read or write this file (parse): not a js file: This file is not "
        "a plain JavaScript/TypeScript locale module (line 1: a call). Only ...",
        "parse",
        "This file is not a plain JavaScript/TypeScript locale module (line 1: a call). Only ...",
    )
    out = ba._import_refusal("js", js)
    assert isinstance(out, InputLimitError) and str(out).startswith("This file is not a plain")
    assert ba._import_refusal("toml", js) is js
    # T5: an encoding refusal reads as the Python filters' sentence (#509), no engine prefix
    enc = ba._fail(
        FakeError(
            "encoding",
            "This file is not valid UTF-8 and could not be read as Western European "
            "(Windows-1252) (byte 0x81 at position 5). Re-save the file as UTF-8.",
        )
    )
    assert isinstance(enc, ba.BilinguaParseError) and enc.code == "encoding", enc
    assert "re-save the file as UTF-8" in str(enc) and "Bilingua" not in str(enc), str(enc)
    # the Python filters' exact opening (read_text_upload): "file is not valid UTF-8 ..."
    assert str(enc).startswith("file is not valid UTF-8"), str(enc)
    # T5: the host's front matter allowlist is a Bilingua option of a new md import
    os.environ.pop("LOC_TMS_FRONT_MATTER_KEYS", None)
    assert ba.install_options("md") == {}
    os.environ["LOC_TMS_FRONT_MATTER_KEYS"] = "Title, keywords"
    try:
        assert ba.install_options("md") == {"markdown.fm_keys": "keywords,title"}
        assert ba.install_options("json") == {}
        f = ba.BilinguaFilter("md")
        assert f._options({}) == {"markdown.fm_keys": "keywords,title"}
        bound = ba.BilinguaFilter("md", {"bilingua": {"options": {}}, "units": []})
        assert bound._options({}) == {}, "a rebuild replays the stamp, not the host setting"
    finally:
        os.environ.pop("LOC_TMS_FRONT_MATTER_KEYS", None)

    # a Rust panic (pyo3 PanicException is a BaseException) is a logged refusal
    class PanicException(BaseException):
        pass

    fake = types.SimpleNamespace(BilinguaError=FakeError)

    def panics():
        raise PanicException("index out of bounds")

    try:
        ba._call(fake, "md", panics)
    except ba.BilinguaParseError as exc:
        assert exc.code == "internal" and "logged" in str(exc)
    else:
        raise AssertionError("a panic must be a refusal")

    def raises():
        raise FakeError("unwritable", "segment u2: no room")

    try:
        ba._call(fake, "md", raises)
    except ba.BilinguaWriteRefused as exc:
        assert "u2" in str(exc)
    else:
        raise AssertionError("unwritable must be BilinguaWriteRefused")


def test_options():
    from app.filters.docx_filter import DOCX_WALK_VERSION
    from app.filters.markdown_filter import MD_WALK_VERSION

    # defaults (what a skeleton without stamps gets) are not options
    assert ba.options_from_kwargs("docx", {"walk": DOCX_WALK_VERSION}) == {}
    assert ba.options_from_kwargs("md", {"md_walk": MD_WALK_VERSION, "fm_keys": None}) == {}
    assert ba.options_from_kwargs("json", {"dialect": "plain"}) == {}
    assert ba.options_from_kwargs("json", {"dialect": "jsonc"}) == {"json.dialect": "jsonc"}
    # option values are str, bool or int (the wheel's FormatOptionValue)
    assert ba.options_from_kwargs("md", {"fm_keys": frozenset({"title", "a"})}) == {
        "markdown.fm_keys": "a,title"
    }
    # W1: an EMPTY allowlist is still a list (no front matter translated), not the default
    assert ba.options_from_kwargs("md", {"fm_keys": frozenset()}) == {"markdown.fm_keys": ""}
    assert ba.options_from_kwargs("docx", {"walk": 4}) == {"docx.walk": "4"}
    # W1: the wheel's image inventory (unit extras) becomes skeleton["a11y_images"]
    doc = {
        "units": [
            {"segments": [{"id": "u1"}], "context": {"extra": {}}},
            {
                "segments": [{"id": "u2"}],
                "context": {"extra": {"locos.a11y_images": "2", "locos.a11y_empty_alt": "a.png\n"}},
            },
            {"segments": [{"id": "u3"}], "context": {"extra": {"locos.a11y_images": "1"}}},
        ]
    }
    locos = [{"segment_id": f"u{i}", "p_index": i - 1} for i in (1, 2, 3)]
    # the adapter reads a DocumentHandle (SPEED2); the stand-in handle over this dict
    from tests._bilingua_doors import PyHandle

    doc = PyHandle(None, doc)
    assert ba.a11y_images(doc, locos) == {
        "images": 3,
        "missing_alt": [
            {"kind": "empty", "unit": 1, "where": "a.png"},
            {"kind": "empty", "unit": 1, "where": ""},
        ],
    }, ba.a11y_images(doc, locos)
    assert (
        ba.a11y_images(PyHandle(None, {"units": [{"segments": [{"id": "u1"}]}]}), locos[:1]) is None
    )
    assert ba.options_from_kwargs("xlsx", {"table": {"cols": 1}}) == {}  # wave 4


# --- wheel part ------------------------------------------------------------------------


MD_PLAIN = b"# Hello\n\nPlain text here.\n"
MD = b"# Hello *world*\n\nSome [link](http://x) text.\n"
PO = (
    b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
    b'msgid "Hi <b>you</b> %s"\nmsgstr ""\n\nmsgid "Bye"\nmsgstr ""\n'
)
JSON = b'{"a": "Hello {name}", "b": "Bye"}\n'


def _bl():
    return ba._module()


def test_wheel_formats():
    ids = {f["id"] for f in _bl().formats()}
    for fmt in WAVES_1_2 + WAVE_3 + WAVE_4 + WAVE_5:
        assert set(ba._FORMATS[fmt][0]) <= ids, (fmt, ba._FORMATS[fmt][0])
    assert ba.profile_stamp() == {"name": "locos", "version": 1}, ba.profile_stamp()


def test_wheel_tokens():
    bl = _bl()
    # the oracle is the wheel's positional rule, exactly (BILI-37)
    for fmt in ("markdown", "json", "po", "txt", "strings", "yaml"):
        for inl in CASES:
            assert ba.inlines_to_locos(inl) == bl.to_locos_text(fmt, inl), (fmt, inl)
            text, cmap = bl.to_locos_text(fmt, inl)
            assert bl.from_locos_text(text, cmap)[0] == inl, (fmt, inl)
        for inl in CASES[1:3]:
            _t, cmap = bl.to_locos_text(fmt, inl)
            for bad in DAMAGE:
                assert ba.locos_to_inlines(bad, cmap) == tuple(bl.from_locos_text(bad, cmap)), (
                    fmt,
                    bad,
                )
    # docx, pptx and xlsx: pairs are r tokens; the tag id is the number
    for fmt in ("docx", "pptx", "xlsx"):
        text, _m = bl.to_locos_text(fmt, CASES[1])
        assert text == f"a {OP}r1{CL}bold{OP}/r1{CL} z", (fmt, text)
        text, _m = bl.to_locos_text(fmt, CASES[3])
        assert text == f"a{OP}x7{CL}never closed", (fmt, text)
    # html pairs by id even when crossed
    text, _m = bl.to_locos_text("html", CASES[5])
    assert text == f"{OP}g1{CL}{OP}g2{CL}{OP}/g1{CL}{OP}/g2{CL}", text
    # through the filter: an html unit skips the number a look-alike in the source uses
    f = ba.BilinguaFilter("html")
    r = f.parse(f"<p>Hi <b>x</b> {OP}g1{CL} z<br/></p>".encode())
    assert r.units[0].text == f"Hi {OP}g2{CL}x{OP}/g2{CL} {OP}g1{CL} z{OP}x3{CL}", r.units[0].text
    assert set(r.units[0].inline_map) == {"2", "3"}, r.units[0].inline_map


def test_ids_and_build():
    os.environ[ENV] = "md,json,po"
    try:
        for fmt, data in (("md", MD), ("json", JSON), ("po", PO)):
            f = get_filter(fmt, new_import=True)
            assert isinstance(f, ba.BilinguaFilter) and f.fmt == fmt
            r = f.parse(data)
            py = get_filter(fmt, _python=True).parse(data)
            got = [(u.p_index, u.text, u.style) for u in r.units]
            exp = [(u.p_index, u.text, u.style or "") for u in py.units]
            assert got == exp, (fmt, got, exp)
            assert [u["p_index"] for u in r.skeleton["units"]] == [u.p_index for u in r.units]
            assert f.build(data, {}) == data, fmt
        f = get_filter("md", new_import=True)
        r = f.parse(MD)
        assert r.skeleton["bilingua"]["segment_ids"] == ["u1", "u2"]
        r2 = f.parse(MD)
        assert [u.text for u in r2.units] == [u.text for u in r.units]
        out = f.build(MD, {"1": "Algo [enlace](http://x) texto."})
        assert out == b"# Hello *world*\n\nAlgo [enlace](http://x) texto.\n", out
        out = f.build(MD, {1: "int keys work too"})
        assert out.endswith(b"int keys work too\n"), out
        out = f.build(MD, {"1": "ignored"}, fallback_keys={"1"})
        assert out == MD, out
        out = f.build_localized(MD, {"0": "Hola *mundo*"}, "es")
        assert out.startswith(b"# Hola *mundo*"), out
        po = get_filter("po", new_import=True)
        out = po.build_localized(PO, {"1": "Adios"}, "fr_FR")
        assert b'msgstr "Adios"' in out, out
        # T4 finding: the old adapter's charset rewrite ate the header's literal \n" after
        # charset=UTF-8 on a localized PO export, merging two header lines. Every header line
        # must stay one quoted string ending in \n.
        assert b'"Content-Type: text/plain; charset=UTF-8\\n"\n' in out, out
        header = out.split(b"\n\n", 1)[0].split(b"\n")[2:]
        assert header and all(h.startswith(b'"') and h.endswith(b'\\n"') for h in header), header
        po_cp = (
            b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=windows-1251\\n"\n'
            b'"Language: ru\\n"\n\nmsgid "' + "Привет".encode("cp1251") + b'"\nmsgstr ""\n'
        )
        rp = get_filter("po", new_import=True).parse(po_cp)
        assert rp.skeleton.get("source_encoding") == "cp1251", rp.skeleton
        out = ba.BilinguaFilter("po", rp.skeleton).build_localized(po_cp, {"0": "Hallo"}, "de-DE")
        assert b'msgstr "Hallo"' in out, out
        for line in out.split(b"\n\n", 1)[0].split(b"\n")[2:]:
            assert line.startswith(b'"') and line.endswith(b'\\n"'), out
    finally:
        os.environ.pop(ENV, None)


def test_build_memo():
    """BILI-18: build() takes the extraction of the byte guard's parse instead of extracting
    the same bytes again; the bytes are the same as a build that extracts on its own."""
    real = ba._module
    calls = []

    class CountedHandle:
        """The DocumentHandle, counting locos_units (SPEED2: a method of the handle)."""

        def __init__(self, h):
            self._h = h

        def locos_units(self):
            calls.append("locos_units")
            return self._h.locos_units()

        def __getattr__(self, name):
            return getattr(self._h, name)

    class Counting:
        def __getattr__(self, name):
            attr = getattr(real(), name)
            if name in ("extract", "extract_handle", "locos_units"):

                def counted(*a, **k):
                    calls.append("extract" if name == "extract_handle" else name)
                    out = attr(*a, **k)
                    return CountedHandle(out) if name == "extract_handle" else out

                return counted
            return attr

    md2 = MD.replace(b"Hello", b"Howdy")
    skel = ba.BilinguaFilter("md").parse(MD).skeleton
    texts = {"1": "Algo [enlace](http://x) texto."}
    raw_build = ba.BilinguaFilter.build.__wrapped__  # the build without the byte guard
    want = raw_build(ba.BilinguaFilter("md", skel), MD, texts)
    want2 = raw_build(ba.BilinguaFilter("md"), md2, texts)  # no stamp: any bytes build
    ba._module = Counting
    try:
        f = ba.BilinguaFilter("md", skel)
        assert f.build(MD, texts) == want
        # guard parse: one extract + one locos_units; the build reuses them (was 2 + 2)
        assert calls == ["extract", "locos_units"], calls
        assert f._memo is None, "the memo is taken once"
        # other bytes than the parse read: the build extracts its own
        calls.clear()
        g = ba.BilinguaFilter("md")
        g.parse(MD)
        assert raw_build(g, md2, texts) == want2
        assert calls == ["extract", "locos_units"] * 2, calls
        # other options (a localized build has a target language): no reuse either
        calls.clear()
        f.parse(MD)
        f.build_localized(MD, texts, "es")
        assert calls == ["extract", "locos_units"] * 2, calls
        # a parse that ends in a refusal keeps nothing
        cap = ba.check_segment_cap

        def refuse(n):
            raise InputLimitError("too many")

        ba.check_segment_cap = refuse
        try:
            f._memo = None
            try:
                f.parse(MD)
                raise AssertionError("parse should refuse")
            except InputLimitError:
                pass
            assert f._memo is None, f._memo
        finally:
            ba.check_segment_cap = cap
    finally:
        ba._module = real


def test_unknown_tokens_kept():
    f = ba.BilinguaFilter("md")
    out = f.build(MD_PLAIN, {"1": f"Texto {OP}g9{CL} aqui."})
    assert f"Texto {OP}g9{CL} aqui.".encode() in out, out
    assert f.last_problems == {"u2": [{"kind": "unknown", "token": f"{OP}g9{CL}"}]}, f.last_problems
    # a look-alike in the source is text in the unit and survives an untouched build
    src = f"# Use {OP}x1{CL} here\n".encode()
    r = f.parse(src)
    assert r.units[0].text == f"Use {OP}x1{CL} here" and r.units[0].inline_map == {}
    assert f.build(src, {}) == src
    assert f.build(src, {"0": f"Usa {OP}x1{CL} aqui"}) == f"# Usa {OP}x1{CL} aqui\n".encode()


def test_pinning():
    os.environ[ENV] = "md,po"
    try:
        r = get_filter("md", new_import=True).parse(MD)
        rp = get_filter("po", new_import=True).parse(PO)
    finally:
        os.environ.pop(ENV, None)
    b = r.skeleton["bilingua"]
    # md and po have binary skeletons: the stamp still names the parser (doc["parser"])
    assert b["crate"] == "bilingua-markdown" and b["version"] and isinstance(b["schema"], int), b
    assert rp.skeleton["bilingua"]["crate"] == "bilingua-po", rp.skeleton["bilingua"]
    assert b["wheel"] and b["format"] == "markdown" and b["original_sha256"]
    assert b["profile"] == {"name": "locos", "version": 1}, b["profile"]
    assert b["segment_ids"] == ["u1", "u2"] and b["units_sha256"]
    assert "source_lang" in b
    # the small stamp: no copy of the Bilingua skeleton
    assert "skeleton" not in b and "skeleton_sha256" not in b, sorted(b)
    # the switch is OFF now: the Bilingua skeleton still gets the adapter, bound to it
    f = get_filter("md", skeleton=r.skeleton)
    assert isinstance(f, ba.BilinguaFilter) and f.stored is not None
    assert f.build(MD, {"0": "Hi *there*"}).startswith(b"# Hi *there*")
    # informational: another crate or wheel version that reads the units the same way builds
    for field, value in (("version", "9.9.9"), ("wheel", "0.1.0"), ("crate", "bilingua-old")):
        ok = copy.deepcopy(r.skeleton)
        ok["bilingua"][field] = value
        out = get_filter("md", skeleton=ok).build(MD, {"0": "Hi"})
        assert out.startswith(b"# Hi\n"), (field, out)
    # binding: another schema or profile, or units that moved, are refused (re-import)
    for field, value in (
        ("schema", 999),
        ("profile", {"name": "other", "version": 1}),
        ("profile", {"name": "locos", "version": 2}),  # the wheel cannot produce version 2
        ("units_sha256", "0" * 64),
        ("segment_ids", ["u1", "u9"]),
    ):
        bad = copy.deepcopy(r.skeleton)
        bad["bilingua"][field] = value
        try:
            get_filter("md", skeleton=bad).build(MD, {"0": "x"})
        except ba.BilinguaParserMismatch as exc:
            assert exc.code == "parser_version" and "Re-import" in str(exc), (field, exc)
        else:
            raise AssertionError(f"stamp {field} {value} was accepted")
    # a changed original is refused, not guessed
    bad = copy.deepcopy(r.skeleton)
    bad["bilingua"]["original_sha256"] = "0" * 64
    try:
        get_filter("md", skeleton=bad).build(MD, {"0": "x"})
    except ba.BilinguaParseError as exc:
        assert exc.code == "invalid_document", exc.code
    else:
        raise AssertionError("a changed original was accepted")
    try:
        f.build(b"# Only one\n", {"0": "x"})
    except ba.BilinguaParseError:
        pass
    else:
        raise AssertionError("mismatched original was accepted")


def test_encoding():
    from app.filters.base import apply_code_page_hint, source_code_page

    f = ba.BilinguaFilter("md")
    # raw bytes: Bilingua reads the legacy file itself
    r = f.parse(b"# Caf\xe9 ok\n")
    assert r.units[0].text == "Caf\xe9 ok", r.units[0].text
    assert r.skeleton.get("source_encoding") == "cp1252", r.skeleton
    assert "text_code_page" not in r.skeleton  # Windows-1252 is the default, never stamped
    r = f.parse(MD)
    assert "source_encoding" not in r.skeleton and "file_warnings" not in r.skeleton
    # decision 22: the project's source language picks the code page; stamped for display
    t = ba.BilinguaFilter("txt")
    ru = "Привет мир\n".encode("cp1251")
    with source_code_page("ru-RU") as hint:
        r = t.parse(ru)
    assert r.units[0].text == "Привет мир", ascii(r.units[0].text)
    assert r.skeleton["bilingua"]["source_lang"] == "ru-RU"
    assert r.skeleton.get("source_encoding") == "cp1251", r.skeleton
    # W2: txt runs the Python filter's decode gate, which sets the hint as the Python filter
    # does; the stamp is ingest's (apply_code_page_hint), as for the Python filter
    with source_code_page("ru-RU") as py_hint:
        py = _py("txt").parse(ru)
    assert {k: v for k, v in r.skeleton.items() if k not in ("units", "bilingua")} == {
        k: v for k, v in py.skeleton.items() if k != "units"
    }, (r.skeleton, py.skeleton)
    assert hint.used == py_hint.used == "cp1251" and hint.warnings == py_hint.warnings
    skel = apply_code_page_hint(copy.deepcopy(r.skeleton), hint)
    assert skel.get("text_code_page") == "cp1251", skel  # ingest keeps the stamp
    # the rebuild uses the stamped source language, whatever hint is around it
    bound = ba.BilinguaFilter("txt", r.skeleton)
    assert bound.build(ru, {}) == ru
    out = bound.build(ru, {"0": "Пока мир"})
    assert out.decode("cp1251").startswith("Пока мир"), out
    # JSON and ARB: strict UTF-8, refused like the Python filter (P-B2), a plain ValueError
    for fmt in ("json", "arb"):
        try:
            ba.BilinguaFilter(fmt).parse(b'{"a": "Caf\xe9"}')
        except ba.BilinguaParseError as exc:
            assert exc.code == "encoding" and "not valid UTF-8" in str(exc), (fmt, exc)
            assert not isinstance(exc, InputLimitError), (fmt, type(exc))
        else:
            raise AssertionError(f"{fmt}: a non-UTF-8 file was read")
    # W2: resjson refuses it as its Python filter does (#509 class, same sentence), json5
    # reads it as every text filter does (Windows-1252), both through their decode gates
    try:
        ba.BilinguaFilter("resjson").parse(b'{"a": "Caf\xe9"}')
    except UnsupportedEncodingError as exc:
        assert "re-save the file as UTF-8" in str(exc), exc
    else:
        raise AssertionError("resjson: a non-UTF-8 file without a BOM was read")
    r = ba.BilinguaFilter("json5").parse(b'{"a": "Caf\xe9"}')
    assert [u.text for u in r.units] == ["Caf\xe9"], r.units
    assert r.skeleton.get("source_encoding") == "cp1252", r.skeleton
    # a UTF-8 BOM is fine
    r = ba.BilinguaFilter("json").parse(b'\xef\xbb\xbf{"a": "Hi"}')
    assert r.units[0].text == "Hi"


def test_empty():
    # W2: zero units is a parse result with no units, as the Python filters give (ingest's
    # #573 door refuses it; allow_empty keeps the zero-unit document an older install held)
    for fmt, data in (
        ("md", b""),
        ("md", b"\n\n"),
        ("json", b"{}"),
        ("po", b""),
        ("txt", b"\n   \n"),
        ("properties", b"# only a comment\nflag\n"),
    ):
        assert _py(fmt).parse(data).units == [], (fmt, data)
        r = ba.BilinguaFilter(fmt).parse(data)
        assert r.units == [] and ba.is_bilingua_skeleton(r.skeleton), (fmt, data, r.units)
    # ...except where the Python filter refuses the file itself: its sentence, same class
    pure = b'<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" Title="T"/>'
    msgs = []
    for f in (_py("xaml"), ba.BilinguaFilter("xaml")):
        try:
            f.parse(pure)
        except EmptyDocumentError as exc:
            msgs.append(str(exc))
        else:
            raise AssertionError(f"{f}: a pure XAML view must be refused")
    assert msgs[0] == msgs[1] and "XAML_VIEW_ATTRIBUTES" in msgs[1], msgs
    try:
        ba.BilinguaFilter("json").parse(b'{"a": ')
    except ba.BilinguaParseError as exc:
        assert exc.code == "parse", exc.code
    else:
        raise AssertionError("a broken JSON file must be refused")


def test_limits():
    import zipfile
    from io import BytesIO

    from app import config

    f = ba.BilinguaFilter("md")
    saved = (config.MAX_UPLOAD_BYTES, config.MAX_DOCUMENT_SEGMENTS, config.ZIP_MAX_MEMBERS)
    try:
        config.MAX_UPLOAD_BYTES = 10
        try:
            f.parse(MD)
        except InputLimitError:
            pass
        else:
            raise AssertionError("upload size cap not applied")
        config.MAX_UPLOAD_BYTES = saved[0]
        config.MAX_DOCUMENT_SEGMENTS = 1
        try:
            f.parse(MD)  # two paragraphs
        except InputLimitError as exc:
            assert "MAX_DOCUMENT_SEGMENTS" in str(exc), exc
        else:
            raise AssertionError("segment cap not applied")
        config.MAX_DOCUMENT_SEGMENTS = saved[1]
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in range(5):
                z.writestr(f"m{n}.xml", "<a/>")
        config.ZIP_MAX_MEMBERS = 2
        try:
            ba.BilinguaFilter("docx").parse(buf.getvalue())
        except InputLimitError:
            pass
        else:
            raise AssertionError("zip member cap not applied")
    finally:
        config.MAX_UPLOAD_BYTES, config.MAX_DOCUMENT_SEGMENTS, config.ZIP_MAX_MEMBERS = saved
    lim = ba.limits_from_config()
    assert lim["max_units"] == config.MAX_DOCUMENT_SEGMENTS
    assert lim["max_zip_entries"] == config.ZIP_MAX_MEMBERS and lim["max_lines"] > 0


def test_wheel_options():
    from app.filters.markdown_filter import MD_WALK_VERSION

    f = ba.BilinguaFilter("md")
    # the keywords export passes are taken by the wheel (no invalid_argument refusal)
    assert f.build(MD, {"0": "Hola"}, md_walk=MD_WALK_VERSION).startswith(b"# Hola")
    assert f.build(MD, {"0": "Hola"}, md_walk=1, fm_keys={"title"}).startswith(b"# Hola")
    r = f.parse(MD, fm_keys={"title"})
    assert r.skeleton["bilingua"]["options"] == {"markdown.fm_keys": "title"}
    # lenient (export re-reading a pre-#509 original) is accepted and ignored
    ba.BilinguaFilter("md").parse(b"# Caf\xe9 ok\n", lenient=True)
    test_wave1_parity()


def _py(fmt):
    return ba._python_filter(fmt)


def test_wave1_parity():
    """W1 blockers: each against the Python filter on the same bytes."""
    from app.filters.base import InputLimitError

    # 1. the front matter allowlist (#731 LOC_TMS_FRONT_MATTER_KEYS) is honoured, and replayed
    fm = (
        b"---\nTitle: T one\ndescription: D one\nkeywords: k one, k two\nsummary: S one\n---\n"
        b"Body text here.\n"
    )
    for keys in ("Title, keywords", ""):
        os.environ["LOC_TMS_FRONT_MATTER_KEYS"] = keys
        try:
            py = _py("md").parse(fm)
            r = ba.BilinguaFilter("md").parse(fm)
            assert [u.text for u in r.units] == [u.text for u in py.units], (keys, r.units)
            assert r.skeleton["md_fm_keys"] == py.skeleton["md_fm_keys"], r.skeleton
            bound = ba.BilinguaFilter("md", r.skeleton)
            texts = {str(u.p_index): "X " + u.text for u in py.units}
        finally:
            os.environ.pop("LOC_TMS_FRONT_MATTER_KEYS", None)
        # export runs with the default host setting: the stamp, not the env, decides
        want = _py("md").build(fm, texts, fm_keys=frozenset(py.skeleton["md_fm_keys"]))
        assert bound.build(fm, texts) == want, (keys, bound.build(fm, texts))
    # 4. md image inventory: the same a11y_images as the Python filter
    md = (
        b'# Pics\n\n![](img/one.png "t")\n\n```\n![](in/fence.png)\n```\n\n'
        b"See ![A cat](cat.png) and ![ ](x/two.gif).\n\n| ![](cell.png) |\n|---|\n"
    )
    py = _py("md").parse(md)
    r = ba.BilinguaFilter("md").parse(md)
    assert r.skeleton.get("a11y_images") == py.skeleton["a11y_images"], (
        r.skeleton.get("a11y_images"),
        py.skeleton["a11y_images"],
    )
    assert [u.style for u in r.units] == [u.style for u in py.units]
    plain = ba.BilinguaFilter("md").parse(b"# No images\n").skeleton
    assert "a11y_images" not in plain
    assert plain["md_fm_keys"] == _py("md").parse(b"# No images\n").skeleton["md_fm_keys"]
    # skeleton metadata export reads: the json resource dialect (absent for generic JSON)
    chrome = b'{"greet": {"message": "Hello", "description": "Toolbar greeting"}}\n'
    for body in (chrome, b'{"a": "Hello"}\n'):
        py = _py("json").parse(body).skeleton.get("dialect")
        assert ba.BilinguaFilter("json").parse(body).skeleton.get("dialect") == py, (body, py)
    bound = ba.BilinguaFilter("json", ba.BilinguaFilter("json").parse(chrome).skeleton)
    assert bound.build_localized(chrome, {"0": "Hallo"}, "de", dialect="chrome") == _py(
        "json"
    ).build_localized(chrome, {"0": "Hallo"}, "de", dialect="chrome")
    # 2. a PO declaring a Python charset name (CP932): read and written back like LocOS
    po = (
        b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=CP932\\n"\n\n'
        b'msgid "Hello"\nmsgstr "\x93\xfa\x96{"\n\nmsgid "World"\nmsgstr ""\n'
    )
    py = _py("po").parse(po)
    r = ba.BilinguaFilter("po").parse(po)
    assert [u.text for u in r.units] == [u.text for u in py.units], r.units
    assert r.skeleton.get("source_encoding") == py.skeleton.get("source_encoding") == "cp932", (
        r.skeleton.get("source_encoding"),
        py.skeleton.get("source_encoding"),
    )
    bound = ba.BilinguaFilter("po", r.skeleton)
    assert bound.build(po, {}) == _py("po").build(po, {}) == po
    texts = {"1": "世界"}
    assert bound.build(po, texts) == _py("po").build(po, texts), bound.build(po, texts)
    # 3. floods with no text: the limit refusal (413), as LocOS, not "no text to translate"
    from app import config

    cap = config.MAX_DOCUMENT_SEGMENTS
    floods = {
        "json": b"[" + b"{}," * (20 * cap + 10) + b"{}]",
        "po": b'msgid "' + b"\\n" * (40 * cap + 10) + b'"\nmsgstr ""\n',
    }
    for fmt, body in floods.items():
        for f in (_py(fmt), ba.BilinguaFilter(fmt)):
            try:
                f.parse(body)
            except InputLimitError as exc:
                assert "MAX_DOCUMENT_SEGMENTS" in str(exc), (fmt, f, str(exc))
                assert not isinstance(exc, ba.EmptyDocumentError), (fmt, f, str(exc))
            else:
                raise AssertionError(f"{fmt} flood parsed by {f}")


def _outcome(f, data: bytes, lang):
    """(refusal class name, message) or (units, skeleton keys beside units, hint state)."""
    from app.filters.base import source_code_page

    try:
        with source_code_page(lang) as hint:
            r = f.parse(data)
    except ValueError as exc:
        # the LocOS refusal class every door branches on (app.filters.base), else ValueError
        base = next((c for c in type(exc).__mro__ if c.__module__ == "app.filters.base"), None)
        return ("refused", base.__name__ if base else "ValueError", str(exc))
    side = {k: v for k, v in r.skeleton.items() if k not in ("units", ba.SKELETON_KEY)}
    return ([u.text for u in r.units], side, hint.used, hint.warnings)


def test_wave2_parity():
    """W2 A-side fixes, each against the Python filter on the same bytes and hint."""
    import io
    import zipfile

    def same(fmt, data, lang=None, classes=None):
        py = _outcome(_py(fmt), data, lang)
        bl = _outcome(ba.BilinguaFilter(fmt), data, lang)
        if classes and py[0] == "refused":
            # refusal parity by class and sentence (the engine reads, the gate refuses)
            assert bl[:2] == py[:2] == ("refused", classes), (fmt, data[:40], py, bl)
            assert bl[2] == py[2], (fmt, py[2], bl[2])
            return
        assert bl == py, (fmt, data[:60], py, bl)

    le, be = b"\xff\xfe", b"\xfe\xff"  # UTF-16 byte order marks
    # decode gates: encoding labels, the decision 22 hint, the #509 refusals
    same("strings", le + '/* c */\n"k" = "Hello";\n'.encode("utf-16-le"))
    same("ini", be + "[s]\nk=Hello\n".encode("utf-16-be"))
    same("properties", b"k = plain ascii\n")
    same("properties", b"k = Caf\xe9\n")
    same("srt", le + "1\n00:00:01,000 --> 00:00:02,000\nHello\n".encode("utf-16-le"))
    for lang in (None, "ja-JP"):
        same("txt", "日本語のテキスト\n".encode("cp932"), lang)
        same("resx", RESX_SJIS, lang)
    bad = "UnsupportedEncodingError"
    same("txt", b"caf\x81 au lait\n", None, bad)
    same("srt", b"1\r\n00:00:01,000 --> 00:00:02,000\r\ncaf\x81\r\n", None, bad)
    same("dtd", '<!ENTITY a "café">'.encode("latin-1"), None, bad)
    same("ftl", "k = café\n".encode("latin-1"), None, bad)
    same("toml", "a = 'café'".encode("latin-1"), None, bad)
    same("toml", "a = 'x'".encode("utf-16"), None, bad)
    same("xcstrings", '{"sourceLanguage": "en", "strings": {"Caf\xe9": {}}}'.encode("latin-1"))
    same("properties", b"k = v\x00\x01\x02\x03\x04\x05" * 50, None, bad)
    same("properties", "k=v\n".encode("utf-16"), None, "InputLimitError")
    same("resjson", '{"a": "Hi"}'.encode("utf-16"))
    # nesting past the engine's cap is the size refusal the Python filter gives (arb: 300 deep)
    deep = ("{" + '"a": {' * 300 + "}" * 300 + "}").encode()
    for f in (_py("arb"), ba.BilinguaFilter("arb")):
        try:
            f.parse(deep)
        except InputLimitError:
            pass
        else:
            raise AssertionError(f"{f}: a 300-deep ARB must be an InputLimitError")
    # a js module that is not a static export: the Python filter's InputLimitError sentence
    same("js", b"const x = compute();\nexport default x;\n", None, "InputLimitError")
    # a `.ts` upload that is XML (Qt Linguist) goes to the ts filter, as the Python js filter
    # hands it over: a Python document, never the Bilingua js parser
    qt = (
        b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n<TS version="2.1" language="de">'
        b"<context><name>Main</name><message><source>Open file</source>"
        b'<translation type="unfinished"></translation></message></context></TS>\n'
    )
    same("js", qt)
    r = ba.BilinguaFilter("js").parse(qt)
    assert not ba.is_bilingua_skeleton(r.skeleton) and [u.text for u in r.units] == ["Open file"]
    out = ba.BilinguaFilter("js").build(qt, {"0": "Datei öffnen"})
    assert out == _py("js").build(qt, {"0": "Datei öffnen"}), out
    # skeleton keys the Python filters record
    same("arb", b'{"@@locale": "en", "hi": "Hello"}')
    same("yaml", b"en:\n  greeting: Hello\n")
    same(
        "xcstrings",
        b'{"sourceLanguage": "en", "version": "1.0", "strings": {"Hello": {}}}',
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fr.xcloc/contents.json", '{"developmentRegion": "en", "targetLocale": "fr"}')
        z.writestr("fr.xcloc/Localized Contents/fr.xliff", XCLOC_XLIFF)
    r = ba.BilinguaFilter("xcloc").parse(buf.getvalue())
    py = _py("xcloc").parse(buf.getvalue())
    for key in ("bundle", "xliff_files", "development_region", "target_locale"):
        assert r.skeleton.get(key) == py.skeleton[key], (key, r.skeleton.get(key), py.skeleton)
    # install options the Python filters read at parse time: stamped, replayed on rebuild
    ftl = b"-brand = Firefox\nhello = Hi { -brand }\n"
    os.environ["FTL_TRANSLATE_TERMS"] = "1"
    try:
        same("ftl", ftl)
        r = ba.BilinguaFilter("ftl").parse(ftl)
        assert r.skeleton[ba.SKELETON_KEY]["options"] == {"ftl.translate_terms": True}
    finally:
        del os.environ["FTL_TRANSLATE_TERMS"]
    same("ftl", ftl)
    bound = ba.BilinguaFilter("ftl", r.skeleton)  # made with terms on: still read with them
    assert bound.build(ftl, {}) == ftl
    from app import config

    view = b'<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" Title="T"/>'
    config.XAML_VIEW_ATTRIBUTES = True
    try:
        same("xaml", view)
    finally:
        config.XAML_VIEW_ATTRIBUTES = False
    print("  [wave 2] decode gates, refusal classes, skeleton keys and install options OK")


FX = os.path.join(_ROOT, "tests", "fixtures", "formats")


def _fx(*parts) -> bytes:
    with open(os.path.join(FX, *parts), "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n")  # autocrlf checkouts: test LF bytes


def _zip(members, comment=b"") -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in members:
            z.writestr(zipfile.ZipInfo(name, date_time=(2026, 11, 7, 12, 0, 0)), body)
        z.comment = comment
    return buf.getvalue()


def test_wave3_parity():
    """W3 A-side fixes (bilingual and CAT formats), each against the Python filter."""
    from app.filters import sdl_package as sp

    def same(fmt, data, cls=None, keys=()):
        py = _outcome(_py(fmt), data, None)
        bl = _outcome(ba.BilinguaFilter(fmt), data, None)
        if cls:
            # refusal parity: the LocOS class every door branches on, and the sentence
            assert py[:2] == ("refused", cls), (fmt, py)
            assert bl == py, (fmt, py, bl)
            return
        assert bl[0] == py[0], (fmt, py[0], bl[0])
        for key in keys:
            assert bl[1].get(key) == py[1].get(key), (fmt, key, py[1], bl[1])

    seg = (
        b'<?xml version="1.0" encoding="windows-1252"?><xliff version="1.2" '
        b'xmlns="urn:oasis:names:tc:xliff:document:1.2"><file original="a" source-language="en" '
        b'datatype="plaintext"><body><trans-unit id="1"><source>Caf\xe9</source><seg-source>'
        b'<mrk mtype="seg" mid="1">Caf\xe9</mrk></seg-source></trans-unit></body></file></xliff>'
    )
    # an encoding the byte-splice build cannot write back: LocOS's InputLimitError and sentence
    same("xliff", seg, "InputLimitError")
    same("sdlxliff", seg, "InputLimitError")
    same("mqxliff", seg, "InputLimitError")
    same("txlf", seg, "InputLimitError")
    # without <seg-source> plain XLIFF reads the same windows-1252 file (the gate lets it by)
    plain = seg.split(b"<seg-source>")[0] + b"</trans-unit></body></file></xliff>"
    same("xliff", plain)
    txml = _fx("wordfast", "sample.txml")
    same("txml", txml.replace(b'encoding="UTF-8"', b'encoding="ISO-8859-1"'), "InputLimitError")
    ttx = _fx("ttx", "sample.ttx")
    same("ttx", ttx.replace(b"encoding='utf-8'", b"encoding='windows-1252'"), "InputLimitError")
    # the #571 pre-parse markup budget: LocOS's MAX_DOCUMENT_SEGMENTS sentence, before any parse
    from app import config

    head = (
        b'<?xml version="1.0" encoding="UTF-8"?><xliff version="1.2" '
        b'xmlns="urn:oasis:names:tc:xliff:document:1.2"><file original="a" '
        b'source-language="en" datatype="plaintext"><body><trans-unit id="1"><source>'
    )
    flood = head + b"<x/>" * (40 * config.MAX_DOCUMENT_SEGMENTS + 10)
    flood += b"</source></trans-unit></body></file></xliff>"
    for fmt in ("xliff", "mxliff"):
        same(fmt, flood, "InputLimitError")
        try:
            ba.BilinguaFilter(fmt).parse(flood)
        except InputLimitError as exc:
            assert "MAX_DOCUMENT_SEGMENTS" in str(exc), (fmt, str(exc))
    # a parse refusal shows the parser's own sentence, as the Python filter does
    v2 = (
        b'<?xml version="1.0" encoding="UTF-8"?><xliff version="2.0" '
        b'xmlns="urn:oasis:names:tc:xliff:document:2.0" srcLang="en"><file id="f"><unit id="1">'
        b"<segment><source>Hi</source></segment></unit></file></xliff>"
    )
    same("mxliff", v2, "ValueError")
    # TTX with nothing to translate: the Python filter's own class and sentence
    unseg = _fx("ttx", "unsegmented.ttx")
    same("ttx", unseg, "EmptyDocumentError")
    try:
        ba.BilinguaFilter("ttx").parse(unseg)
    except EmptyDocumentError as exc:
        from app.filters.ttx_filter import UnsegmentedTtxError

        assert isinstance(exc, UnsegmentedTtxError), type(exc)
    done = ttx
    for empty in (b'<Tuv Lang="DE-DE"></Tuv>', b'<Tuv Lang="DE-DE"/>'):
        done = done.replace(empty, b'<Tuv Lang="DE-DE">x</Tuv>')
    done = done.replace(b"here. </Tuv></Tu>", b'here. </Tuv><Tuv Lang="DE-DE">y</Tuv></Tu>')
    done = done.replace(b" 42</Tuv></Tu>", b' 42</Tuv><Tuv Lang="DE-DE">42</Tuv></Tu>')
    same("ttx", done, "EmptyDocumentError")
    # TTX text outside every <Tu>: the Python skeleton key, not a file warning
    same("ttx", ttx, keys=("unsegmented_text", "file_warnings"))
    assert _py("ttx").parse(ttx).skeleton.get("unsegmented_text"), "fixture needs loose text"
    # package archives: LocOS's own zip checks and sentences (label per package format)
    xlf = _fx("xlzpack", "content.xlf")
    for fmt in ("xlz", "mqxlz"):
        same(fmt, b"not a zip", "InputLimitError")
        same(fmt, _zip([("../../evil.xlf", xlf)]), "InputLimitError")
        same(fmt, _zip([("notes.txt", b"hi")]), "InputLimitError")
        same(fmt, _zip([("a.xlf", seg)]), "InputLimitError")
    # Trados package documents: the member blob's sdl_package record; a raw package with
    # two target files refused with LocOS's sentence
    proj, de = _fx("sdlpackage", "demo.sdlproj"), _fx("catxliff", "sample.sdlxliff")
    fr = de.replace(b'target-language="de-DE"', b'target-language="fr-FR"')
    two = _zip(
        [
            ("demo.sdlproj", proj),
            ("de-DE/guide.docx.sdlxliff", de),
            ("fr-FR/guide.docx.sdlxliff", fr),
        ]
    )
    same("sdlrpx", two, "InputLimitError")
    info = sp.inspect(two, "demo.sdlppx")
    blob = sp.member_blob(two, info, info.targets[0])
    same("sdlrpx", blob, keys=("sdl_package",))
    r = ba.BilinguaFilter("sdlrpx").parse(blob)
    assert r.skeleton["sdl_package"]["member"] == "de-DE/guide.docx.sdlxliff", r.skeleton
    assert [u.text for u in r.units] == [u.text for u in _py("sdlrpx").parse(blob).units]
    one = _zip([("demo.sdlproj", proj), ("de-DE/guide.docx.sdlxliff", de)])
    same("sdlrpx", one, keys=("sdl_package",))
    test_wave3_w3b()
    print("  [wave 3] decode and budget gates, package checks, refusal sentences, keys OK")


def test_wave3_w3b():
    """W4c, with the W3 B-side wheel: the package listing from the member slot warnings,
    nesting caps coded `limit`, and the CAT tag repairs shown as LocOS's QA flag."""
    from app.services.quality import tag_damage, tag_flag
    from app.services.segment import ph_close, ph_open

    # 1. xlz / mqxlz: skeleton["package"] as the Python package filter records it (member
    # path, p_index offset = earlier members' slots, units per member, other files), and the
    # engine's locos_member_slots warnings are no file warnings
    mq, sdl, ws = (
        _fx("catxliff", "sample.mqxliff"),
        _fx("catxliff", "sample.sdlxliff"),
        _fx("xlzpack", "content.xlf"),
    )
    tmx = b'<?xml version="1.0"?><tmx version="1.4"><header/><body/></tmx>'
    pkgs = [
        ("mqxlz", _zip([("document.mqxliff", mq), ("skeleton", b"\x00\x01"), ("p.html", b"<p/>")])),
        ("xlz", _zip([("content.xlf", ws), ("content.skl", b"<skl>{1}</skl>")])),
        (
            "xlz",
            _zip(
                [
                    ("tm/project.tmx", tmx),
                    ("fr-FR/guide.docx.sdlxliff", sdl),
                    ("fr-FR/help.html.xlf", ws),
                ]
            ),
        ),
    ]
    for fmt, pkg in pkgs:
        py = _py(fmt).parse(pkg)
        r = ba.BilinguaFilter(fmt).parse(pkg)
        assert r.skeleton["package"] == py.skeleton["package"], (fmt, r.skeleton, py.skeleton)
        assert [u.p_index for u in r.units] == [u.p_index for u in py.units], fmt
        kinds = {w.get("kind") for w in r.skeleton.get("file_warnings") or []}
        assert "locos_member_slots" not in kinds, r.skeleton["file_warnings"]
        assert r.skeleton.get("file_warnings") == py.skeleton.get("file_warnings"), fmt
    assert r.skeleton["package"]["xliff_files"][1]["offset"] > 0, r.skeleton["package"]

    # 2. nesting caps: json (and the office crates) refuse with code `limit`, LocOS's class
    # (as the Python filter: InputLimitError, 413 on /api)
    deep = b'{"a":' * 1500 + b'"x"' + b"}" * 1500
    for f in (ba.BilinguaFilter("json"), _py("json")):
        try:
            f.parse(deep)
            raise AssertionError("deep json should refuse")
        except InputLimitError as exc:
            assert "nest" in str(exc).lower(), str(exc)

    # 3. CAT tag repairs (B8): the export is written as LocOS writes it (no refusal), and the
    # repaired segment gets the tag_integrity flag LocOS's QA shows for that target
    xlf = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<xliff version="1.2"><file source-language="en" target-language="de">'
        b'<body><trans-unit id="1"><source>Please <g id="1">Click</g> here</source>'
        b"</trans-unit></body></file></xliff>"
    )
    g1, g1c = ph_open("g", 1), ph_close("g", 1)
    cases = [
        ("repeated", f"bitte {g1}Klick{g1c} hier {g1}doppelt{g1c} jetzt"),
        ("stray_close", f"bitte {g1c}{g1}Klick{g1c} hier"),
        ("unclosed", f"bitte {g1}Klick hier"),
        ("pair_as_point", f"bitte {OP}x1{CL}Klick hier"),
    ]
    for kind, new in cases:
        f = ba.BilinguaFilter("xliff")
        out = f.build(xlf, {"0": new})
        assert out == _py("xliff").build(xlf, {"0": new}) or kind == "pair_as_point", (kind, out)
        kinds = [p["kind"] for v in f.last_problems.values() for p in v]
        assert kind in kinds, (kind, f.last_problems)
        src = _py("xliff").parse(xlf).units[0].text
        want = tag_damage(src, new)
        assert want, (kind, "LocOS QA flags this target")
        assert f.last_qa_flags == {"0": [tag_flag(want)]}, (kind, f.last_qa_flags)
    # the sdlxliff case of verify_cat_xliff_filter: same bytes as LocOS, one flag
    seg0 = min(u.p_index for u in _py("sdlxliff").parse(sdl).units)
    new = f"{g1}A {OP}g7{CL}B{OP}/g7{CL} {g1}C"
    f = ba.BilinguaFilter("sdlxliff")
    assert f.build(sdl, {str(seg0): new}) == _py("sdlxliff").build(sdl, {str(seg0): new})
    assert list(f.last_qa_flags) == [str(seg0)], f.last_qa_flags
    assert f.last_qa_flags[str(seg0)][0]["type"] == "tag_integrity"
    # a clean target: no flag; a token kept as text only (#814) is no repair either
    f = ba.BilinguaFilter("xliff")
    f.build(xlf, {"0": f"bitte {g1}Klick{g1c} hier"})
    assert f.last_qa_flags == {} and f.last_problems == {}, f.last_problems
    f.build(xlf, {"0": f"bitte {g1}Klick{g1c} {OP}g9{CL}"})
    assert f.last_qa_flags == {} and f.last_problems, f.last_problems
    print("  [wave 3 w3b] package listing, limit-coded nesting caps, tag repair QA flags OK")


def _docx(paras=("Body text.",), table=None, comment=None, external=False) -> bytes:
    """A python-docx document: paragraphs, an optional table (header + rows), an optional
    comment on the first paragraph, an optional external image relationship and field."""
    import io

    from docx import Document as Docx

    d = Docx()
    first = None
    for text in paras:
        p = d.add_paragraph(text)
        first = first or p
    if comment:
        d.add_comment(first.runs, text=comment, author="Ann", initials="A")
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, val in enumerate(row):
                t.cell(r, c).text = val
    if external:
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        from lxml import etree

        d.part.relate_to("https://example.invalid/image.png", RT.IMAGE, True)
        w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        fld = etree.SubElement(d.add_paragraph("Field ")._p, f"{{{w}}}fldSimple")
        fld.set(f"{{{w}}}instr", ' INCLUDEPICTURE "https://example.invalid/f" ')
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _xlsx(rows) -> bytes:
    import io

    from openpyxl import Workbook

    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _members(data: bytes) -> dict:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: z.read(i) for i in z.infolist()}


def test_wave4_parity():
    """W4 A-side fixes (Office and DTP), each against the Python filter on the same bytes."""
    from app import config

    def outcome(f, data, **kw):
        try:
            r = f.parse(data, **kw)
        except ValueError as exc:
            base = next((c for c in type(exc).__mro__ if c.__module__ == "app.filters.base"), None)
            return ("refused", base.__name__ if base else type(exc).__name__, str(exc))
        side = {k: v for k, v in r.skeleton.items() if k not in ("units", ba.SKELETON_KEY)}
        return ([(u.p_index, u.text, u.style) for u in r.units], side, r.skeleton["units"])

    def same(fmt, data, cls=None, keys=(), **kw):
        py = outcome(_py(fmt), data, **kw)
        bl = outcome(ba.BilinguaFilter(fmt), data, **kw)
        if cls:
            # "*": whatever class the Python filter refuses with (its own error types)
            assert py[0] == "refused" and cls in ("*", py[1]), (fmt, py)
            assert bl == py, (fmt, py, bl)
            return bl
        assert bl[0] == py[0], (fmt, py[0], bl[0])
        for key in keys:
            assert bl[1].get(key) == py[1].get(key), (fmt, key, py[1].get(key), bl[1].get(key))
        return bl

    docx = _docx()
    # 1. the zip guard with the Python filter's label ("This DOCX file has ... internal parts")
    many = _zip([(f"p{i}.xml", b"<a/>") for i in range(config.ZIP_MAX_MEMBERS + 1)])
    for fmt in ("docx", "pptx", "odt", "ods", "vsdx", "idml", "xcloc"):
        same(fmt, many, "InputLimitError")
    # 2. a refusal of Bilingua's goes to the Python filter: its own class and sentence (not a
    #    zip, the DTD rule #814.4, an OpenDocument package without content.xml, an IDML
    #    package without designmap.xml, a Visio file without pages, InCopy DOCTYPE)
    for fmt in ("docx", "xlsx", "pptx", "odt", "odp", "vsdx", "idml"):
        try:
            _py(fmt).parse(b"not a zip at all")
        except ValueError:
            same(fmt, b"not a zip at all", "*")
        except Exception:  # the Python filter crashes (BadZipFile): the adapter still refuses
            got = outcome(ba.BilinguaFilter(fmt), b"not a zip at all")
            assert got[0] == "refused", (fmt, got)
    m = _members(docx)
    m["word/document.xml"] = m["word/document.xml"].replace(
        b"?>", b'?><!DOCTYPE w:document [<!ENTITY e "x">]>', 1
    )
    same("docx", _zip(list(m.items())), "InputLimitError")
    same("odt", _zip([("mimetype", b"application/vnd.oasis.opendocument.text")]), "InputLimitError")
    same("idml", _zip([("mimetype", b"application/vnd.adobe.indesign-idml-package")]), "*")
    same("vsdx", _zip([("[Content_Types].xml", b"<Types/>")]), "InputLimitError")
    icml = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><Document/>'
    same("icml", icml, "*")
    # MIF: UTF-8 only (the #509 class, 422 with the re-save sentence)
    same(
        "mif", b"<MIFFile 2019>\n<Para <ParaLine <String `Caf\xe9'>>>\n", "UnsupportedEncodingError"
    )
    # 3. the #571 markup budget before any tree is built
    flood = _members(docx)
    flood["word/document.xml"] = flood["word/document.xml"].replace(
        b"<w:body>", b"<w:body>" + b"<w:p/>" * (40 * config.MAX_DOCUMENT_SEGMENTS + 10), 1
    )
    same("docx", _zip(list(flood.items())), "InputLimitError")
    # 4. comments (#690): the per-project option reaches the parser, both ways, as in LocOS
    commented = _docx(comment="Please check this sentence.")
    for on in (True, False):
        bl = same("docx", commented, keys=("comments",), comments=on)
        assert any("Please check" in u[1] for u in bl[0]) is on, (on, bl[0])
    # the #690 skeleton record ("comments": True when offered), as the XLSX filter keeps it
    noted = _xlsx([["Total"]])
    same("xlsx", noted, keys=("comments",), comments=True)
    assert ba.BilinguaFilter("xlsx").parse(noted, comments=True).skeleton["comments"] is True
    assert ba.BilinguaFilter("docx").takes_comments and ba.BilinguaFilter("xlsx").takes_columns
    # 5. XLSX column mode: the units, the note columns as unit notes, the target column written
    sheet = _xlsx(
        [
            ["id", "en", "de", "fr", "notes"],
            ["a", "Save file", None, "Enregistrer", "button"],
            ["b", "Open file", None, None, None],
        ]
    )
    cols = {
        "columns": "source=en,target.de=de,target.fr=fr,key=id,context=notes",
        "source_locale": "en",
        "target_locales": ["de", "fr"],
    }
    bl = same("xlsx", sheet, **cols)
    py_units = _py("xlsx").parse(sheet, **cols).skeleton["units"]
    assert [u.get("note") for u in bl[2]] == [u.get("note") for u in py_units], bl[2]
    stamped = ba.BilinguaFilter("xlsx").parse(sheet, **cols).skeleton
    out = ba.BilinguaFilter("xlsx", stamped).build_localized(sheet, {"0": "Sauver"}, "fr")
    import io

    from openpyxl import load_workbook

    ws = load_workbook(io.BytesIO(out)).active
    assert ws["D2"].value == "Sauver" and ws["B2"].value == "Save file", ws["D2"].value
    # 6. DOCX file warnings: external resources (docx_resources) and the Storyline notice
    same("docx", _docx(external=True), keys=("file_warnings",))
    assert _py("docx").parse(_docx(external=True)).skeleton.get("file_warnings"), "needs one"
    story = _docx(
        paras=(),
        table=[
            ["ID", "Original Text", "Translation"],
            ["5aBc.Name", "Welcome", "Welcome"],
            ["6kLm", "Well done!", "Well done!"],
        ],
    )
    same("docx", story, keys=("file_warnings",))
    # 7. targets pass through xml_target_text (#243): a lone surrogate or control character
    #    cannot reach the engine; Word's vertical tab is a line break
    sk = ba.BilinguaFilter("docx").parse(docx).skeleton
    out = ba.BilinguaFilter("docx", sk).build(docx, {"0": "A\ud800B\x00C"})
    assert [u.text for u in _py("docx").parse(out).units] == ["ABC"], _py("docx").parse(out).units
    # 8. no Python walk stamp on a Bilingua document (export replays the Bilingua stamp only)
    for fmt, data in (("docx", docx), ("xlsx", sheet)):
        sk = ba.BilinguaFilter(fmt).parse(data).skeleton
        assert not {"docx_walk", "xlsx_walk", "pptx_walk"} & set(sk), sk.keys()
    print("  [wave 4] zip labels, Python refusals, budgets, comments, column mode, warnings OK")


RESX_SJIS = (
    '<?xml version="1.0" encoding="shift_jis"?>\n<root>\n  <data name="a" xml:space="preserve">'
    "\n    <value>日本語</value>\n  </data>\n</root>\n"
).encode("shift_jis")
XCLOC_XLIFF = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2"'
    ' version="1.2">\n  <file original="en.lproj/Localizable.strings" source-language="en"'
    ' target-language="fr" datatype="plaintext">\n    <body>\n      <trans-unit id="hi">\n'
    "        <source>Hello</source>\n      </trans-unit>\n    </body>\n  </file>\n</xliff>\n"
)


def _refusal(fn, *args, **kw):
    """(class name of the app.filters.base class it derives from, or its own name, message)."""
    try:
        fn(*args, **kw)
    except ValueError as exc:
        return type(exc).__name__, str(exc)
    return None


_EPUB_OPF = (
    b'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
    b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata>'
    b'<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
    b'<spine><itemref idref="c1"/></spine></package>'
)
_EPUB_CONTAINER = (
    b'<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
    b' version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf"'
    b' media-type="application/oebps-package+xml"/></rootfiles></container>'
)
_CHAPTER = (
    b'<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
    b'<body><p>Hello <img src="a.png" alt="A cat"/> world</p></body></html>'
)


def _epub(chapter=_CHAPTER, mimetype=b"application/epub+zip"):
    return _zip(
        [
            ("mimetype", mimetype),
            ("META-INF/container.xml", _EPUB_CONTAINER),
            ("OEBPS/content.opf", _EPUB_OPF),
            ("OEBPS/c1.xhtml", chapter),
        ]
    )


_EML = (
    b"From: a@example.com\r\nTo: b@example.com\r\nSubject: Hello\r\nMIME-Version: 1.0\r\n"
    b'Content-Type: text/html; charset="utf-8"\r\n\r\n<p>Hello <b>world</b></p>\r\n'
)

# Upload refusals LocOS makes (security and safety), each with the bytes that trigger it.
_W5_REFUSALS = [
    # DTD internal subset (entity expansion / XXE) in every XML dialect
    ("xml", b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r><p>&x;</p></r>'),
    ("dita", b'<!DOCTYPE topic [<!ENTITY e "x">]><topic id="t"><title>&e;</title></topic>'),
    ("docbook", b'<!DOCTYPE article [<!ENTITY e "x">]><article><para>&e;</para></article>'),
    # XSLT: not well-formed (its internal subset is read, never expanded, as in LocOS)
    (
        "xslt",
        b'<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
        b'<xsl:template match="/"><p>Hi</xsl:template></xsl:stylesheet>',
    ),
    (
        "xmb",
        b'<!DOCTYPE messagebundle [<!ENTITY e "x">]><messagebundle>'
        b'<msg id="1">&e;</msg></messagebundle>',
    ),
    (
        "ttml",
        b'<!DOCTYPE tt><tt xmlns="http://www.w3.org/ns/ttml"><body><div><p>Hi</p>'
        b"</div></body></tt>",
    ),
    # Android strings: internal subset; ITS: nesting deeper than MAX_DEPTH
    (
        "xml",
        b'<!DOCTYPE resources [<!ENTITY e "x">]><resources><string name="a">&e;</string>'
        b"</resources>",
    ),
    ("xml", b"<a>" * 150 + b"x" + b"</a>" * 150),
    # server pages: code used as an attribute name, unclosed code, mixed PHP and <% %> in .html
    ("aspx", b'<input <%= disabled %> value="x">'),
    ("phtml", b'<div <?php echo $a; ?>="x">Hi</div>'),
    ("jsp", b"<p>Hi <% out.print(1);</p>"),
    ("html", b"<p><?php echo 1; ?> and <% x %></p>"),
    ("php", b"<?php\n$a = 1;\nfunction f() { return 2; }\n"),
    # e-mail: not a message, signed message
    ("eml", b"%PDF-1.4 not an email"),
    ("eml", b"Subject: p\n\n-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nHi\n"),
    # packages: an EPUB without its mimetype, a zip that is not a Flare project
    ("epub", None),
    ("flprjzip", None),
]


def test_wave5_gates():
    """W5 upload gates (no wheel needed): every Python refusal of the table above comes from
    the adapter's gate with the Python filter's class and sentence, and an engine code."""
    for fmt in WAVE_5:
        assert fmt in ba._DECODE_GATES and fmt in ba._REFUSAL_BY_PYTHON, fmt
    for fmt in ("epub", "flprjzip", "xml", "dita", "docbook", "xslt", "xmb", "xtb", "ttml"):
        assert fmt in ba._XML_TARGET, fmt
    for fmt, raw in _W5_REFUSALS:
        if raw is None:
            raw = _epub(mimetype=b"text/plain") if fmt == "epub" else _zip([("a.txt", b"x")])
        py = _refusal(_py(fmt).parse, raw)
        assert py is not None, (fmt, raw[:60])
        got = _refusal(ba.decode_gate, fmt, raw)
        assert got == py, (fmt, py, got)
    # the label the Python filter records (#509, decision 22)
    page = '<html><head><meta charset="windows-1251"></head><body><p>Привет</p></body></html>'
    raw = page.encode("cp1251")
    want = _py("html").parse(raw).skeleton.get("source_encoding")
    assert ba.decode_gate("html", raw) == want and want, want


def test_wave5_guards():
    """W5 export guards (no wheel needed): markup a translation would add is refused as the
    Python writers refuse it (UnsafeMarkupRefused), a translated text change passes."""
    from app.filters.html_guard import UnsafeMarkupRefused

    def refused(guard, before, after, locale=None):
        try:
            guard(before, after, locale)
        except UnsafeMarkupRefused:
            return True
        return False

    html = ba._BUILD_GUARDS["html"]
    page = b'<p>Hello <img src="a.png" alt="A cat"> world</p>'
    assert not refused(html, page, page.replace(b"Hello", b"Hallo").replace(b"A cat", b"Eine"))
    assert refused(html, page, page.replace(b'alt="A cat"', b'alt="x" onerror="alert(1)"'))
    assert refused(html, page, page + b"<script>alert(1)</script>")
    # a legacy page written in UTF-8 by the writer: the charset label is not markup
    legacy = b'<meta charset="windows-1252"><p>Caf\xe9</p>'
    assert not refused(html, legacy, '<meta charset="UTF-8"><p>Café</p>'.encode())
    # server pages: no new code, the page structure with code opaque; a moved code block is fine
    for fmt, src, bad in (
        (
            "phtml",
            b"<p>Hi <?php echo $a; ?> there</p>",
            b"<p>Hi <?php echo $a; ?> <?php system('x'); ?></p>",
        ),
        ("jsp", b"<p>Hi ${user.name} there</p>", b"<p>Hi ${user.name} ${applicationScope}</p>"),
        ("aspx", b"<p>Hi <%= Name %> there</p>", b"<p>Hi <%= Name %> <% Response.End(); %></p>"),
    ):
        g = ba._BUILD_GUARDS[fmt]
        assert refused(g, src, bad), fmt
        assert not refused(g, src, src.replace(b"Hi", b"Hallo")), fmt
    moved_b = b'<p>A <%= x %> <a href="<%= url %>" title="t">go</a>.</p>'
    moved_a = b'<p>A  <a href="<%= url %>" title="t">geh</a>.<%= x %></p>'
    assert not refused(ba._BUILD_GUARDS["erb"], moved_b, moved_a)
    # HTML Help sitemap
    hhc = (
        b'<HTML><BODY><UL><LI><OBJECT type="text/sitemap"><param name="Name" value="Intro">'
        b"</OBJECT></UL></BODY></HTML>"
    )
    g = ba._BUILD_GUARDS["hhc"]
    assert not refused(g, hhc, hhc.replace(b"Intro", b"Einleitung"))
    assert refused(g, hhc, hhc.replace(b'value="Intro"', b'value="x" onclick="alert(1)"'))
    # EPUB / Flare members, and the package file list
    g = ba._BUILD_GUARDS["epub"]
    ok = _epub(_CHAPTER.replace(b"Hello", b"Hallo"))
    assert not refused(g, _epub(), ok)
    assert refused(g, _epub(), _epub(_CHAPTER.replace(b"</p>", b"</p><script>x()</script>")))
    assert refused(g, _epub(), _zip([*_members(_epub()).items(), ("OEBPS/x.js", b"x")]))
    # e-mail: the MIME tree and header fields stay; the HTML part is checked (#676)
    g = ba._BUILD_GUARDS["eml"]
    assert not refused(g, _EML, _EML.replace(b"world", b"Welt").replace(b"Hello", b"Hallo"))
    assert refused(
        g, _EML, _EML.replace(b"Subject: Hello", b"Subject: Hello\r\nBcc: x@example.com")
    )
    assert refused(g, _EML, _EML.replace(b"<b>world</b>", b'<b onclick="x()">world</b>'))
    # XML: no new kind of element, attribute, comment or PI; dropping a tag is fine
    g = ba._BUILD_GUARDS["dita"]
    xml = b'<topic id="t"><title>Hi <b>there</b></title></topic>'
    assert not refused(g, xml, b'<topic id="t"><title>Hallo there</title></topic>')
    assert refused(g, xml, b'<topic id="t"><title>Hi <script>x</script></title></topic>')
    assert refused(g, xml, b'<topic id="t"><title>Hi <b onclick="x">t</b></title></topic>')
    assert refused(g, xml, b'<topic id="t"><title>Hi <!--x--></title></topic>')
    assert refused(g, xml, b'<topic id="t"><title>Hi <b>there</title></topic>')
    # a localized build declares the language; a multilingual layout adds its language element
    assert not refused(
        g, xml, b'<topic id="t" xml:lang="ar" dir="rtl"><title>x</title></topic>', "ar"
    )
    ml = b"<items><item><en>Hello</en></item></items>"
    g = ba._BUILD_GUARDS["xml"]
    assert not refused(g, ml, b"<items><item><en>Hello</en><ar>x</ar></item></items>", "ar")
    assert refused(g, ml, b"<items><item><en>Hello</en><ar>x</ar></item></items>", "de-DE")
    # XMB exports as an XTB bundle
    xmb = b'<messagebundle><msg id="1">Hi</msg></messagebundle>'
    xtb = (
        b'<translationbundle lang="de"><translation id="1">Hallo</translation></translationbundle>'
    )
    assert not refused(ba._BUILD_GUARDS["xmb"], xmb, xtb)
    # Bilingua's own #676 refusal (code write / unwritable) is LocOS's UnsafeMarkupRefused
    for code in ("write", "unwritable"):
        err = Exception(
            "cannot write: This page cannot be exported safely: in the translated file a web"
        )
        err.code, err.detail = code, str(err)
        got = ba._fail(err, "html")
        assert isinstance(got, UnsafeMarkupRefused), (code, got)
        assert str(got).startswith("This page cannot be exported safely"), str(got)
        assert got.code == code


def test_wave5_parity():
    """W5 with the wheel: the upload refusals as LocOS makes them, the export checks on what
    Bilingua writes, the .html server dialect (#672), the HTML drift check, ITS notes."""
    from app.filters.html_guard import UnsafeMarkupRefused

    for fmt, raw in _W5_REFUSALS:
        if raw is None:
            raw = _epub(mimetype=b"text/plain") if fmt == "epub" else _zip([("a.txt", b"x")])
        py = _refusal(_py(fmt).parse, raw)
        bl = _refusal(ba.BilinguaFilter(fmt).parse, raw)
        assert bl == py, (fmt, raw[:60], py, bl)
    # an attack in a translated alt text: refused or written inert, never live markup
    page = b'<p>Hello <img src="a.png" alt="A cat"> world</p><!--> <p title="x">t</p>'
    f = ba.BilinguaFilter("html")
    res = f.parse(page)
    attack = 'x" onfocus=alert(1) autofocus y="'
    mapping = {
        str(u.p_index): (attack if u.style.startswith("a11y") else u.text) for u in res.units
    }
    g = ba.BilinguaFilter("html", res.skeleton)
    try:
        out = g.build(page, mapping)
    except UnsafeMarkupRefused:
        out = None
    if out is not None:
        from app.filters.html_guard import browser_views

        for view in browser_views(out.decode()):
            for st in view.starts:
                assert "onfocus" not in dict(st.attrs), (st.name, st.attrs)
    # #672: a .html page with closed PHP code is read by the PHP template parser
    php_page = b"<p>Hello <?php echo $name; ?> and welcome</p>"
    res = ba.BilinguaFilter("html").parse(php_page)
    assert res.skeleton.get("server_code") == _py("html").parse(php_page).skeleton["server_code"]
    assert all("<?php" not in u.text for u in res.units), [u.text for u in res.units]
    # the .php router: a page template is read by the PHP template parser, as LocOS reads it
    tpl = b"<html><body><p>Hello <?php echo $name; ?>, welcome back.</p></body></html>"
    py_units = [u.text for u in _py("php").parse(tpl).units]
    res = ba.BilinguaFilter("php").parse(tpl)
    assert len(res.units) == len(py_units) and all("<?php" not in u.text for u in res.units), (
        py_units,
        [u.text for u in res.units],
    )
    out = ba.BilinguaFilter("php", res.skeleton).build(tpl, {"0": "Hallo ⟦x1⟧, willkommen."})
    assert b"<?php echo $name; ?>" in out and b"Hallo" in out, out
    # export's HTML drift check: stored indices never address a different extraction
    src = b"<div><p>Hello world</p></div>"
    res = ba.BilinguaFilter("html").parse(src)
    tampered = {**res.skeleton, "units": [{"p_index": 0, "text": "Another text"}]}
    try:
        ba.BilinguaFilter("html", tampered).build(src, {"0": "Hola mundo"})
    except ba.BilinguaParserMismatch as exc:
        assert "Re-import" in str(exc)
    else:
        raise AssertionError("a changed HTML extraction was exported")
    assert ba.BilinguaFilter("html", res.skeleton).build(src, {"0": "Hola mundo"}) == (
        b"<div><p>Hola mundo</p></div>"
    )
    # ITS locNote: the unit note, as the Python filter records it
    its = (
        b'<doc xmlns:its="http://www.w3.org/2005/11/its" its:version="2.0">'
        b'<p its:locNote="Keep it short">Save</p></doc>'
    )
    py_notes = [u.get("note") for u in _py("xml").parse(its).skeleton["units"]]
    bl_notes = [u.get("note") for u in ba.BilinguaFilter("xml").parse(its).skeleton["units"]]
    assert bl_notes == py_notes, (py_notes, bl_notes)


WAVE_6 = ["adoc", "rst", "wiki", "dokuwiki", "tex", "ass", "sbv", "sub", "scc", "stl", "stf"]
WAVE_6 += ["dxf", "rc", "desktop", "catkeys", "plist", "stringsdict", "mo", "csv", "tsv", "xtg"]
WAVE_6 += ["ts"]
_BINARY = b"\x00\xff\x01\xfe" * 40
_XXE = b'<?xml version="1.0"?><!DOCTYPE %s [<!ENTITY e "x">]>'


def _w6_refusals():
    from app.filters.subtitle_text_filter import _VOBSUB_MAGIC

    return [
        # the #509 decode refusal of the text formats (binary bytes)
        *[(fmt, _BINARY) for fmt in ("rst", "wiki", "dokuwiki", "tex", "ass", "sbv")],
        # an OpenPGP .asc is not AsciiDoc
        ("adoc", b"-----BEGIN PGP SIGNATURE-----\n\niQEzBAEBCAAdFiEE\n"),
        # a VobSub .sub is refused as such, before any text decode
        ("sub", _VOBSUB_MAGIC + _BINARY),
        ("scc", b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n"),
        ("stl", b"solid cube\n  facet normal 0 0 1\n" * 20),
        ("stf", b"Key\tSource\nk\ta\x00b\n"),
        ("dxf", b"AutoCAD Binary DXF\r\n\x1a\x00" + _BINARY),
        # an .rc that switches code pages cannot be read in one encoding
        (
            "rc",
            b'#pragma code_page(1252)\nSTRINGTABLE\nBEGIN\n 1 "Caf\xe9"\nEND\n'
            b'#pragma code_page(1251)\nSTRINGTABLE\nBEGIN\n 2 "\xcf\xf0"\nEND\n',
        ),
        ("desktop", b"[Desktop Entry]\nType=Application\nName=Caf\xe9\n"),
        ("catkeys", b"1\tenglish\tapp\t123\nHello\tctx\t\tHallo\x00\n"),
        # XXE: internal subsets and entities in a property list, a stringsdict, a Qt file
        (
            "plist",
            _XXE % b"plist" + b"<plist><dict><key>a</key><string>&e;</string></dict></plist>",
        ),
        (
            "stringsdict",
            _XXE % b"plist" + b"<plist><dict><key>a</key><string>&e;</string></dict></plist>",
        ),
        (
            "ts",
            _XXE % b"TS"
            + b"<TS><context><name>a</name><message><source>&e;</source></message></context></TS>",
        ),
        ("mo", b"this is not a compiled gettext catalog at all"),
        ("xtg", b"\xff\xfe<\x00v"),
        # the cheap plist and SCC gates word every pre-scan refusal as the Python filter
        ("plist", b"{ CFBundleName = App; }"),
        ("plist", b"bplist01" + b"\x00" * 40),
        ("plist", b'<?xml version="1.0" encoding="ISO-8859-1"?><plist><string>x</string></plist>'),
        ("stringsdict", b"\xff\xfe<\x00p\x00\x00\xd8"),
        ("scc", b"Scenarist_SCC V1.0\n\n00:00:01:00\t" + b"9420 " * 4100 + b"\n"),
    ]


def test_wave6_gates():
    """W6 upload gates (no wheel needed): every Python refusal of the table above comes from
    the adapter's gate with the Python filter's class and sentence; the labels it records;
    the wave-6 tables, options and the Qt Linguist (`ts`) routing through the js entry."""
    for fmt in WAVE_6:
        assert fmt in ba._FORMATS and fmt in ba._DECODE_GATES, fmt
        assert fmt in ba._REFUSAL_BY_PYTHON and fmt in ba._WRITE_REFUSAL_BY_PYTHON, fmt
    assert type(ba._python_filter("ts")).__name__ == "QtTsFilter"
    for fmt, raw in _w6_refusals():
        py = _refusal(_py(fmt).parse, raw)
        assert py is not None, (fmt, raw[:60])
        got = _refusal(ba.decode_gate, fmt, raw)
        assert got == py, (fmt, py, got)
    # the source_encoding label the Python filter records (#509 / decision 22, BOM read v2)
    for fmt, raw in (
        ("csv", "key,source\ngreeting,Caf\xe9\n".encode("cp1252")),
        ("sbv", b"\xef\xbb\xbf0:00:01.000,0:00:02.000\nCaf\xe9\n"),
        ("rc", b'STRINGTABLE\nBEGIN\n 1 "Caf\xe9"\nEND\n'),
        ("wiki", "Caf\xe9 au lait.\n".encode("cp1252")),
        ("stf", "Key\tSource\nk\tCaf\xe9\n".encode("cp1252")),
        (
            "plist",
            b"\xff\xfe"
            + '<?xml version="1.0"?><plist><string>Hi</string></plist>'.encode("utf-16-le"),
        ),
    ):
        want = _py(fmt).parse(raw).skeleton.get("source_encoding") or "utf-8"
        assert ba.decode_gate(fmt, raw) == want, (fmt, want, ba.decode_gate(fmt, raw))
    # options: STF lists no targets (LocOS's unit list); WIKI_TEMPLATE_PARAMS; CSV planning
    from app import config

    assert ba.install_options("stf") == {"stf.load_targets": False}
    old = config.WIKI_TEMPLATE_PARAMS
    config.WIKI_TEMPLATE_PARAMS = "Quote:text"
    try:
        assert ba.install_options("wiki") == {"wiki.template_params": "Quote:text"}
    finally:
        config.WIKI_TEMPLATE_PARAMS = old
    assert ba.install_options("wiki") == ({} if not old else {"wiki.template_params": old})
    kw = {"columns": None, "source_locale": "en", "target_locales": ["de-DE", "fr"]}
    assert ba.options_from_kwargs("csv", kw) == {"csv.target_locales": "de-DE,fr"}
    kw = {"columns": "source=en, target.de=de", "target_locales": ["de"]}
    assert ba.options_from_kwargs("tsv", kw) == {
        "csv.columns": "source=en, target.de=de",
        "csv.target_locales": "de",
    }
    # a Python writer's refusal raised for the adapter is both classes, with its sentence
    from app.filters.scc_filter import CaptionTargetError

    wrapped = ba._write_refusal(CaptionTargetError("Cue 2 was not exported: empty."))
    assert isinstance(wrapped, CaptionTargetError) and isinstance(wrapped, ba.BilinguaWriteRefused)
    assert str(wrapped) == "Cue 2 was not exported: empty." and wrapped.code == "unwritable"
    # Qt Linguist: a stamped `ts` document reached through the js entry is bound to `ts`;
    # with only `ts` switched on, a TypeScript module stays with the Python js filter
    stamped = {"format": "ts", "units": [], "bilingua": {"format": "ts"}}
    assert ba.BilinguaFilter("js", stamped).fmt == "ts"
    assert ba.BilinguaFilter("js", {"bilingua": {"format": "js"}}).fmt == "js"
    qt = b'<?xml version="1.0"?><TS version="2.1"><context><name>a</name></context></TS>'
    module = b"export default { hello: 'Hello' };\n"
    os.environ[ENV] = "ts"
    try:
        ts_only = ba.BilinguaFilter("js", modules=False)
        assert type(ts_only._handoff(module)).__name__ == "JsModuleFilter"
        handed = ts_only._handoff(qt)
        assert isinstance(handed, ba.BilinguaFilter) and handed.fmt == "ts", handed
        assert ba.BilinguaFilter("js")._handoff(module) is None  # js on: the adapter reads it
        if ba.available():
            chosen = ba.select("js", None, new_import=True)
            assert isinstance(chosen, ba.BilinguaFilter) and not chosen._modules
        os.environ[ENV] = "js"
        assert type(ba.BilinguaFilter("js")._handoff(qt)).__name__ == "QtTsFilter"
    finally:
        os.environ.pop(ENV, None)
    # a Qt file is exported through LocOS's js filter: an identity build is the upload
    assert ba.BilinguaFilter("ts").writes_targets is False


def test_wave6_parity():
    """W6 with the wheel: upload refusals as LocOS makes them, the Python writers' export
    refusals (class and sentence, also through export), Qt Linguist through js, CSV planning."""
    from app.filters.base import EmptyDocumentError
    from app.filters.scc_filter import CaptionTargetError

    for fmt, raw in _w6_refusals():
        py = _refusal(_py(fmt).parse, raw)
        bl = _refusal(ba.BilinguaFilter(fmt).parse, raw)
        assert bl == py, (fmt, raw[:60], py, bl)

    def write_refusal(fmt, data, mapping):
        res = ba.BilinguaFilter(fmt).parse(data)
        try:
            ba.BilinguaFilter(fmt, res.skeleton).build(data, mapping)
        except ValueError as exc:
            return exc
        raise AssertionError(f"{fmt}: {mapping} was written")

    sbv = b"0:00:01.000,0:00:02.000\nHello there\n\n0:00:03.000,0:00:04.000\nSecond cue\n"
    scc = (
        b"Scenarist_SCC V1.0\n\n00:00:01:00\t9420 9420 94ae 94ae 9452 9452 97a1 97a1 c845 "
        b"4c4c 4f80 942f 942f\n"
    )
    for fmt, data, mapping in (
        ("sbv", sbv, {"1": ""}),
        ("sbv", sbv, {"0": "0:00:05.000,0:00:06.000"}),
        ("scc", scc, {"0": "中文"}),
        ("scc", scc, {"0": "x " * 90}),
    ):
        exc = write_refusal(fmt, data, mapping)
        want = _refusal(_py(fmt).build, data, mapping)
        assert isinstance(exc, CaptionTargetError) and isinstance(exc, ba.BilinguaWriteRefused)
        assert (want[0], str(exc)) == ("CaptionTargetError", want[1]), (fmt, want, str(exc))
    # a .desktop file with nothing to translate: the Python filter's own sentence
    empty = b"[Desktop Entry]\nType=Application\nExec=app\n"
    py = _refusal(_py("desktop").parse, empty)
    try:
        ba.BilinguaFilter("desktop").parse(empty)
    except EmptyDocumentError as exc:
        assert (type(exc).__name__, str(exc)) == py, (py, str(exc))
    else:
        raise AssertionError("an empty .desktop file was imported")
    # Qt Linguist through the js entry: Bilingua's ts reader, same units, identity = upload
    qt = (
        b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n<TS version="2.1" language="vi">\n'
        b"<context>\n    <name>A</name>\n    <message>\n        <source>Style name</source>\n"
        b"        <translation>T\xc3\xaan</translation>\n    </message>\n</context>\n</TS>\n"
    )
    os.environ[ENV] = "ts"
    try:
        res = ba.BilinguaFilter("js", modules=False).parse(qt)
    finally:
        os.environ.pop(ENV, None)
    assert res.skeleton["bilingua"]["format"] == "ts", res.skeleton["bilingua"]
    assert [u.text for u in res.units] == [u.text for u in get_filter("ts").parse(qt).units]
    g = get_filter("ts", skeleton=res.skeleton)
    assert isinstance(g, ba.BilinguaFilter) and g.fmt == "ts"
    assert g.build(qt, {"0": "Style name"}) == get_filter("ts").build(qt, {"0": "Style name"})
    # CSV with the project's target languages: the header names the target column
    csv = b"key,en,de\nsave,Save,\nopen,Open,\n"
    kw = {"columns": None, "source_locale": "en", "target_locales": ["de"]}
    py = _py("csv").parse(csv, **kw)
    res = ba.BilinguaFilter("csv").parse(csv, **kw)
    assert [u.text for u in res.units] == [u.text for u in py.units], res.units
    out = ba.BilinguaFilter("csv", res.skeleton).build_localized(
        csv, {"0": "Speichern", "1": "Offnen"}, "de"
    )
    assert out == _py("csv").build_localized(
        csv, {"0": "Speichern", "1": "Offnen"}, "de", table=py.skeleton["table"]
    ), out
    # CSV formula injection (OWASP): a written cell starting = + - @ TAB (or fullwidth =) gets
    # an apostrophe, through the stamped document as LocOS writes it
    multi = b"\xef\xbb\xbfkey;en;de;fr\r\nhello;Hello;;Bonjour\r\nbad;Plus one;;\r\n"
    kw = {"columns": None, "source_locale": "en", "target_locales": ["de", "fr"]}
    py = _py("csv").parse(multi, **kw)
    res = ba.BilinguaFilter("csv").parse(multi, **kw)
    g = ba.BilinguaFilter("csv", res.skeleton)
    for bad in ("=cmd|' /C calc'!A0", "+1+1", "-2+3", "@SUM(1)", "\tTab", "＝X"):
        want = _py("csv").build_localized(multi, {"1": bad}, "de", table=py.skeleton["table"])
        got = g.build_localized(multi, {"1": bad}, "de")
        assert got == want and ("'" + bad).encode() in got, (bad, got, want)
    # a build with no stamp given the Python column plan (`table=`) writes the same columns
    unstamped = ba.BilinguaFilter("csv").build_localized(
        multi, {"1": "Plus eins"}, "de", table=py.skeleton["table"]
    )
    assert unstamped == _py("csv").build_localized(
        multi, {"1": "Plus eins"}, "de", table=py.skeleton["table"]
    ), unstamped


_RU_NAME = "Введение"
_HHC = (
    "<HTML><HEAD></HEAD><BODY>\n<UL>\n"
    '<LI> <OBJECT type="text/sitemap">\n'
    f'<param name="Name" value="{_RU_NAME}">\n'
    '<param name="Local" value="intro.htm">\n'
    "</OBJECT>\n</UL>\n</BODY></HTML>\n"
).encode("cp1251")


def test_final2_options():
    """FINAL2 (no wheel): the HTML Help project code page, the LaTeX inline commands, both
    kinds of missing-alt findings, HTML Help write refusals routed to the Python writer."""
    # decision 21: hhp_code_page becomes htmlhelp.code_page for .hhc/.hhk only (a .hhp reads
    # its own Language=); a bool or a missing value is no code page
    assert ba.options_from_kwargs("hhc", {"hhp_code_page": 1251}) == {"htmlhelp.code_page": 1251}
    assert ba.options_from_kwargs("hhk", {"hhp_code_page": 932}) == {"htmlhelp.code_page": 932}
    assert ba.options_from_kwargs("hhp", {"hhp_code_page": 1251}) == {}
    assert ba.options_from_kwargs("hhc", {"hhp_code_page": None}) == {}
    assert ba.options_from_kwargs("hhc", {"hhp_code_page": True}) == {}
    # LATEX_TEXT_COMMANDS: the names the Python filter accepts, as tex.text_commands
    old = os.environ.get("LATEX_TEXT_COMMANDS")
    try:
        os.environ.pop("LATEX_TEXT_COMMANDS", None)
        assert ba.install_options("tex") == {}
        os.environ["LATEX_TEXT_COMMANDS"] = " mynote, keyword ,bad-name,x1 "
        assert ba.install_options("tex") == {"tex.text_commands": "mynote,keyword"}
    finally:
        if old is None:
            os.environ.pop("LATEX_TEXT_COMMANDS", None)
        else:
            os.environ["LATEX_TEXT_COMMANDS"] = old
    # html / server pages / mdx / docx: locos.a11y_missing_alt (both kinds, file order) wins
    # over the empty-alt names; a malformed value falls back to them; unknown kinds skipped
    from tests._bilingua_doors import PyHandle

    found = '[{"kind": "missing", "where": "a.png"}, {"kind": "empty", "where": "b.png"}, '
    found += '{"kind": "odd", "where": "c.png"}]'
    doc = {
        "units": [
            {"segments": [{"id": "u1"}], "context": {"extra": {}}},
            {
                "segments": [{"id": "u2"}],
                "context": {
                    "extra": {
                        "locos.a11y_images": "3",
                        "locos.a11y_empty_alt": "b.png",
                        "locos.a11y_missing_alt": found,
                    }
                },
            },
            {
                "segments": [{"id": "u3"}],
                "context": {
                    "extra": {
                        "locos.a11y_images": "1",
                        "locos.a11y_empty_alt": "d.png",
                        "locos.a11y_missing_alt": "not json",
                    }
                },
            },
        ]
    }
    locos = [{"segment_id": f"u{i}", "p_index": i - 1} for i in (1, 2, 3)]
    got = ba.a11y_images(PyHandle(None, doc), locos)
    assert got == {
        "images": 4,
        "missing_alt": [
            {"kind": "missing", "unit": 1, "where": "a.png"},
            {"kind": "empty", "unit": 1, "where": "b.png"},
            {"kind": "empty", "unit": 2, "where": "d.png"},
        ],
    }, got
    # HTML Help: Bilingua's `unwritable` (a character the code page cannot hold) goes to the
    # Python writer, whose HtmlHelpEncodingError is raised (wave-6 mechanism)
    assert {"hhc", "hhk", "hhp"} <= ba._WRITE_REFUSAL_BY_PYTHON

    # a wheel with the handle API that refuses these options as unknown (speed2 and older)
    # counts as not installed; one that reads the probe files, or refuses them for their
    # content, has them
    class Refused(Exception):
        def __init__(self, code, msg):
            super().__init__(msg)
            self.code = code

    def wheel(error):
        def extract_handle(name, data, **kw):
            if error is not None:
                raise error
            return object()

        mod = types.SimpleNamespace(**{n: (lambda *a, **k: None) for n in ba._REQUIRED_API})
        mod.extract_handle = extract_handle
        return mod

    old_wheel = Refused("invalid_argument", "format option tex.text_commands: no format has it")
    assert ba._knows_options(wheel(old_wheel)) is False
    assert ba._knows_options(wheel(Refused("parse", "not an HTML Help file"))) is True
    assert ba._knows_options(wheel(None)) is True


def test_final2_parity():
    """FINAL2 with the combined wheel: HTML Help read in the project code page and its write
    refusal as LocOS's class and sentence; html / server page / mdx missing-alt findings of
    both kinds equal LocOS's; LATEX_TEXT_COMMANDS honoured; the subtitle refusal sentence."""
    from app.filters.htmlhelp_filter import HtmlHelpEncodingError

    # decision 21: an undeclared .hhc in the project's code page (cp1251), stamped as LocOS
    py = _py("hhc").parse(_HHC, hhp_code_page=1251)
    res = ba.BilinguaFilter("hhc").parse(_HHC, hhp_code_page=1251)
    assert [u.text for u in res.units] == [u.text for u in py.units] == [_RU_NAME], res.units
    assert res.skeleton.get("hhp_code_page") == py.skeleton["hhp_code_page"] == 1251
    assert res.skeleton["bilingua"]["options"] == {"htmlhelp.code_page": 1251}
    plain = ba.BilinguaFilter("hhc").parse(_HHC)  # no project file: Windows-1252, no stamp
    assert [u.text for u in plain.units] == [u.text for u in _py("hhc").parse(_HHC).units]
    assert "hhp_code_page" not in plain.skeleton
    # export replays the stamped code page (export hands a Bilingua document no keyword)
    g = ba.BilinguaFilter("hhc", res.skeleton)
    out = g.build_localized(_HHC, {"0": "Глава"}, "ru-RU")
    want = _py("hhc").build_localized(_HHC, {"0": "Глава"}, "ru-RU", hhp_code_page=1251)
    assert out == want and "Глава".encode("cp1251") in out, (out, want)
    # a translation cp1251 cannot hold: LocOS's HtmlHelpEncodingError, class and sentence
    want = _refusal(_py("hhc").build_localized, _HHC, {"0": "中文"}, "ru-RU", hhp_code_page=1251)
    try:
        g.build_localized(_HHC, {"0": "中文"}, "ru-RU")
    except HtmlHelpEncodingError as exc:
        assert isinstance(exc, ba.BilinguaWriteRefused) and exc.code == "unwritable"
        assert (want[0], str(exc)) == ("HtmlHelpEncodingError", want[1]), (want, str(exc))
    else:
        raise AssertionError("a translation cp1251 cannot hold was written")
    hhp = b"[OPTIONS]\r\nLanguage=0x419 Russian\r\nTitle=" + _RU_NAME.encode("cp1251") + b"\r\n"
    res = ba.BilinguaFilter("hhp").parse(hhp)
    assert [u.text for u in res.units] == [u.text for u in _py("hhp").parse(hhp).units]
    want = _refusal(_py("hhp").build_localized, hhp, {"0": "中文"}, "ru-RU")
    g = ba.BilinguaFilter("hhp", res.skeleton)
    got = _refusal(g.build_localized, hhp, {"0": "中文"}, "ru-RU")
    assert want and got == ("BilinguaHtmlHelpEncodingError", want[1]), (want, got)
    # HTMLHELP_UTF8_FALLBACK (read when the file is written, never stamped): UTF-8 as LocOS
    from app import config

    g = ba.BilinguaFilter("hhc", ba.BilinguaFilter("hhc").parse(_HHC, hhp_code_page=1251).skeleton)
    old = getattr(config, "HTMLHELP_UTF8_FALLBACK", False)
    config.HTMLHELP_UTF8_FALLBACK = True
    try:
        fresh = ba.BilinguaFilter("hhc").parse(_HHC, hhp_code_page=1251)
        assert "htmlhelp.utf8_fallback" not in fresh.skeleton["bilingua"].get("options", {})
        out = g.build_localized(_HHC, {"0": "中文"}, "ru-RU")
        want = _py("hhc").build_localized(_HHC, {"0": "中文"}, "ru-RU", hhp_code_page=1251)
        assert out == want and "中文".encode() in out, (out, want)
    finally:
        config.HTMLHELP_UTF8_FALLBACK = old
    # missing-alt findings, both kinds, as the Python filters record them
    page = (
        b'<html><body><p>First <img src="a.png"> one</p><p>Second</p>'
        b'<img src="b.png" alt=""><p>Third <img src="c.png" alt="A cat"></p></body></html>'
    )
    for fmt, data in (
        ("html", page),
        ("aspx", page.replace(b"<p>Second</p>", b"<p>Second <%= Name %></p>")),
        ("mdx", b'# Title\n\nText ![](a.png) here.\n\n<img src="b.png" />\n\nMore text.\n'),
    ):
        want = _py(fmt).parse(data).skeleton.get("a11y_images")
        got = ba.BilinguaFilter(fmt).parse(data).skeleton.get("a11y_images")
        assert want and got == want, (fmt, want, got)
        assert any(m["kind"] == "missing" for m in want["missing_alt"]), (fmt, want)
    # LATEX_TEXT_COMMANDS: an install's own inline command is text, as LocOS reads it
    tex = b"\\documentclass{article}\n\\begin{document}\nSee \\mynote{the margin note} here.\n"
    tex += b"\\end{document}\n"
    old = os.environ.get("LATEX_TEXT_COMMANDS")
    try:
        os.environ["LATEX_TEXT_COMMANDS"] = "mynote"
        want = [(u.text, u.inline_map) for u in _py("tex").parse(tex).units]
        res = ba.BilinguaFilter("tex").parse(tex)
        assert [u.text for u in res.units] == [w[0] for w in want], (res.units, want)
        assert res.skeleton["bilingua"]["options"] == {"tex.text_commands": "mynote"}
    finally:
        if old is None:
            os.environ.pop("LATEX_TEXT_COMMANDS", None)
        else:
            os.environ["LATEX_TEXT_COMMANDS"] = old
    without = [u.text for u in ba.BilinguaFilter("tex").parse(tex).units]
    assert without == [u.text for u in _py("tex").parse(tex).units], without
    # decision 22: a legacy file is read in the project's source-language code page; where
    # Bilingua reads it in another one (ini, yaml, js read Windows-1252 before Bilingua
    # 0.9.1), the Python filter imports it (a Python document, no garbled
    # text); where they agree Bilingua imports it. Which one a wheel does is read from the
    # wheel itself on the same bytes (the source encoding it reports), never from a version.
    from app.filters.base import source_code_page

    bl = ba._module()
    ru = "Добро пожаловать"
    for fmt, text, probe in (
        ("ini", f"[m]\ntitle={ru}\n", True),
        ("yaml", f"title: {ru}\n", True),
        ("js", f'export default {{ title: "{ru}" }};\n', True),
        ("txt", f"{ru}.\n", False),
    ):
        data = text.encode("cp1251")
        bilingua_doc = True
        if probe:  # does this wheel read the file in the source-language code page?
            raw = ba.BilinguaFilter(fmt)._extract(bl, data, source_lang="ru-RU")
            bilingua_doc = not ba._other_code_page("cp1251", raw.source_encoding)
        with source_code_page("ru-RU"):
            res = ba.BilinguaFilter(fmt).parse(data)
            want = [u.text for u in _py(fmt).parse(data).units]
        assert [u.text for u in res.units] == want and ru in want[0], (fmt, res.units, want)
        assert ba.is_bilingua_skeleton(res.skeleton) is bilingua_doc, (fmt, res.skeleton)
        if probe and bilingua_doc:  # the A1 wheel: LocOS's reading, stamped as LocOS does
            assert res.skeleton.get("source_encoding") == "cp1251", (fmt, res.skeleton)
    # decision 22 warnings: ingest does not apply the hint to a Bilingua skeleton, so the
    # adapter puts the hint's encoding warning on file_warnings (a Western file in a Greek
    # project: read as Windows-1252, with LocOS's warning)
    west = "Café crème brûlée, déjà vu.\n\nNaïve résumé.\n".encode("cp1252")
    with source_code_page("el-GR") as hint:
        _py("txt").parse(west)
        want = [dict(w) for w in hint.warnings]
    with source_code_page("el-GR"):
        res = ba.BilinguaFilter("txt").parse(west)
    assert ba.is_bilingua_skeleton(res.skeleton), res.skeleton
    got = [w for w in res.skeleton.get("file_warnings") or [] if w["kind"] == "text_encoding"]
    assert want and got == want, (want, got)
    assert not ba._other_code_page("cp932", "Shift_JIS")
    assert not ba._other_code_page("cp1252", "ISO-8859-1")
    assert not ba._other_code_page("utf-8", "windows-1252")
    assert ba._other_code_page("cp1251", "windows-1252")
    # st-4: Bilingua's own caption refusal now carries LocOS's sentence (routing stays on
    # `unwritable` and the Python writer, whose class export shows)
    sbv = b"0:00:01.000,0:00:02.000\nHello there\n\n0:00:03.000,0:00:04.000\nSecond cue\n"
    res = ba.BilinguaFilter("sbv").parse(sbv)
    want = _refusal(_py("sbv").build, sbv, {"1": ""})
    try:
        ba.BilinguaFilter("sbv", res.skeleton).build(sbv, {"1": ""})
    except ba.BilinguaWriteRefused as exc:
        engine = exc.__cause__  # the adapter's refusal made from the wheel's error
        root = engine.__cause__  # bilingua.BilinguaError
        assert str(exc) == want[1], (str(exc), want)
        assert getattr(root, "code", None) == "unwritable", root
        detail = getattr(root, "detail", None) or str(root)
        assert detail == want[1], ("st-4: the wheel's sentence", detail, want[1])
    else:
        raise AssertionError("an emptied cue was written")


def test_export_errors():
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.models import Document, Project, Segment, SegStatus, TargetLocale, TransMode
    from app.services import export
    from app.services.export_gate import ExportUnreadableBlob
    from app.services.ingest import ingest_document

    init_db()
    os.environ[ENV] = "md"
    try:
        with Session(engine) as s:
            p = Project(
                name="bili-export", org_id=1, source_locale="en-US", default_provider="mock"
            )
            s.add(p)
            s.commit()
            s.refresh(p)
            s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
            s.commit()
            doc = ingest_document(s, p, "a.md", MD_PLAIN, TransMode.SEGMENTED)
            s.commit()
            doc_id = doc.id
            assert "bilingua" in (doc.skeleton or {}), "a new import under the switch is Bilingua"
            assert doc.skeleton["bilingua"]["source_lang"] == "en-US", doc.skeleton["bilingua"]
            for seg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
                seg.target_text = "Bonjour"
                seg.status = SegStatus.MT
                s.add(seg)
            s.commit()
            # an empty file is refused at the ingest door, before any row is written
            try:
                ingest_document(s, p, "empty.md", b"\n\n", TransMode.SEGMENTED)
            except EmptyDocumentError:
                s.rollback()
            else:
                raise AssertionError("an empty Bilingua import must be EmptyDocumentError")
    finally:
        os.environ.pop(ENV, None)
    with Session(engine) as s:
        out = export.build_translated_file(s, s.get(Document, doc_id), "fr-FR")
        assert b"Bonjour" in out, out
        # a tampered stamp: a clean refusal
        d = s.get(Document, doc_id)
        good = copy.deepcopy(d.skeleton)
        bad = copy.deepcopy(d.skeleton)
        bad["bilingua"]["schema"] = 999
        d.skeleton = bad
        s.add(d)
        s.commit()
        try:
            export.build_translated_file(s, s.get(Document, doc_id), "fr-FR")
        except ExportUnreadableBlob:
            pass
        else:
            raise AssertionError("a parser mismatch must be ExportUnreadableBlob")
        d = s.get(Document, doc_id)
        d.skeleton = good
        s.add(d)
        s.commit()
        # the module missing: the same clean refusal
        real = sys.modules.get("bilingua")
        sys.modules["bilingua"] = None
        try:
            try:
                export.build_translated_file(s, s.get(Document, doc_id), "fr-FR")
            except ExportUnreadableBlob as exc:
                assert "not installed" in str(exc)
            else:
                raise AssertionError("a missing module must be ExportUnreadableBlob")
        finally:
            if real is None:
                sys.modules.pop("bilingua", None)
            else:
                sys.modules["bilingua"] = real
    # an existing document with a None, {} or Python skeleton keeps the Python filter while
    # the switch is on
    os.environ[ENV] = "md"
    try:
        assert ba.select("md", None) is None and ba.select("md", {}) is None
        assert ba.select("md", {"units": []}) is None
    finally:
        os.environ.pop(ENV, None)


def test_fallback_missing_module():
    real = sys.modules.get("bilingua")
    # a wheel without the T1 token API is "not installed" for the adapter
    for stand_in in (None, types.ModuleType("bilingua")):
        sys.modules["bilingua"] = stand_in  # None makes `import bilingua` raise ImportError
        try:
            assert not ba.available()
            os.environ[ENV] = ",".join(FORMATS)
            for fmt in FORMATS:
                assert not isinstance(get_filter(fmt, new_import=True), ba.BilinguaFilter), fmt
            assert get_filter("md").fmt == "md"
            stamped = {"bilingua": {"crate": "bilingua-markdown"}, "units": []}
            try:
                get_filter("md", skeleton=stamped)
            except ba.BilinguaUnavailable as exc:
                assert "not installed" in str(exc)
            else:
                raise AssertionError("a Bilingua document must say the module is missing")
        finally:
            os.environ.pop(ENV, None)
            if real is None:
                sys.modules.pop("bilingua", None)
            else:
                sys.modules["bilingua"] = real


def main():
    test_default_off()
    test_formats()
    test_oracle()
    test_error_table()
    test_options()
    test_wave5_gates()
    test_wave5_guards()
    test_wave6_gates()
    test_final2_options()
    if ba.available():
        test_wheel_formats()
        test_wheel_tokens()
        test_ids_and_build()
        test_build_memo()
        test_unknown_tokens_kept()
        test_pinning()
        test_encoding()
        test_empty()
        test_limits()
        test_wheel_options()
        test_wave2_parity()
        test_wave3_parity()
        test_wave4_parity()
        test_wave5_parity()
        test_wave6_parity()
        test_final2_parity()
        test_export_errors()
        print("bilingua wheel checks: ran")
    else:
        print("bilingua module not installed: wheel-dependent checks skipped")
    test_fallback_missing_module()
    print("BILINGUA ADAPTER OK")


if __name__ == "__main__":
    main()
