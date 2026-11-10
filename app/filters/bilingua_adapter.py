"""Bilingua adapter: a Localization OS Filter backed by the Bilingua Rust parsing engine.

Bilingua (a PyO3 wheel, module `bilingua`) parses a file into a Document of units, inline
codes and a skeleton, and merges a translated Document back into the file. This module puts
the LocOS `Filter` interface on top of it (D27).

THE SWITCH. Per format, ON by default (`all`): the install-wide setting `bilingua_formats`, a
comma list of canonical LocOS formats ("md,json,po"), which the env var
`LOC_TMS_BILINGUA_FORMATS` overrides (`enabled_formats`). `get_filter`
(app/filters/base.py) returns this
adapter in two cases only: the stored skeleton carries the `bilingua` stamp (the document
was made here, pinned to its parser), or the caller passes `new_import=True` (the one first
parse of an upload, services/ingest.py) and the format is listed and the `bilingua` module
imports. Every other call, with no skeleton, an empty one or a Python one, gets the Python
filter, so an existing document keeps the filter that made it (connectors, zip batches,
quality checks). With the setting empty nothing in the pipeline changes.

THE ENGINE DOES THE LOCOS WORK. Every call asks Bilingua for the `locos` compatibility
profile, so each format behaves as the LocOS filter does. The raw upload bytes go to
Bilingua with the project's source language (decision 22 picks the fallback code page from
it); Bilingua decodes, and its writers keep the file's encoding. Units, p_index, style and
token text come from `bilingua.locos_units`, which numbers tokens by the LocOS rule of each
format (BILI-37: positional, html pairs by id, docx/pptx/xlsx tag id as number, numbers that
look-alike source text uses are skipped). Translations go back through
`bilingua.locos_set_targets`: unknown and look-alike tokens stay in the text as typed (#814);
for the CAT formats a repeated, stray, unclosed or pair-as-point token is repaired as LocOS's
writers repair it, and the segment gets LocOS's tag_integrity QA flag (`last_qa_flags`).
The adapter never numbers or parses tokens itself; `inlines_to_locos` / `locos_to_inlines`
below are a Python oracle of the positional rule that verify_bilingua_adapter compares with
the wheel, never on the build path.

SKELETON. `skeleton["bilingua"]` is a small stamp: format, crate, version, schema, wheel,
profile {name, version}, source_lang, segment ids (and p_indexes when they are not
positions), a hash of the units, the sha256 of the original and the format options. A build
re-extracts the stored original with the stamped profile version and source language and
checks, in order: the original is the same bytes; profile name, profile version and schema
match (a wheel that cannot produce the stamped profile version refuses); the units are the
same (segment ids and the units hash). The crate and wheel versions are informational: a
newer wheel that reads the document the same way builds it, one that reads it differently
refuses with BilinguaParserMismatch ("re-import"). Never rebuilt silently by a different
parser. Also on the skeleton, as for the Python filters: `file_warnings` (Bilingua's
warnings), `source_encoding` and `text_code_page` (decision 22, for display).

ERRORS (plan 5.6). Every BilinguaError code maps to one LocOS refusal: `limit` (and a nesting
cap) -> InputLimitError; zero units -> an empty parse result as the Python filters give
(ingest refuses it, #573; xaml's filter-level refusal keeps its own sentence); `parse`,
`unsupported`, `encoding`, `invalid_argument`, `io` -> BilinguaParseError (on a new import
`encoding` is BilinguaEncodingError, also the #509 UnsupportedEncodingError, and a js module
that is not a static export the Python filter's InputLimitError); `parser_version` ->
BilinguaParserMismatch;
`invalid_document`, `write` -> BilinguaParseError with re-import text; `unwritable` and the
codes that mean an adapter bug (`locked`, `invented_tag`, `duplicated_tag`,
`unknown_segment`) -> BilinguaWriteRefused (a plain ValueError: the Python filters' export
refusal path), the bugs logged with the format; `internal` or a Rust panic -> logged,
BilinguaParseError "the parser failed on this file". No fallback to a Python filter on a
refusal.

INPUT LIMITS. LocOS's budgets (upload bytes, zip guard for package formats, segment count and
length) run before and after Bilingua, and are also passed to Bilingua as its B6 `limits`,
with LocOS's numbers. Wave-2 formats also run the Python filter's decode step first
(`_DECODE_GATES`): the #509 refusals, the line budget, the `source_encoding` label and the
decision 22 hint come out exactly as from the Python filter.

Documented in: docs/architecture/translation-pipeline.md
"""

import codecs
import contextlib
import hashlib
import json
import logging
import os
import re
import zipfile

from app import config
from app.filters.base import (
    EmptyDocumentError,
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    UnsupportedEncodingError,
    check_segment_cap,
    check_unit_texts,
    get_filter,
)

ENV_VAR = "LOC_TMS_BILINGUA_FORMATS"
TRACE_VAR = "LOC_TMS_BILINGUA_TRACE"  # test aid: append "<pid> <op> <fmt>" per call to this file
# Test aid for the parity runner: suites call get_filter(fmt) directly (no new_import), so with
# this set every call without a skeleton counts as a new import. Never set in production.
ASSUME_NEW_VAR = "LOC_TMS_BILINGUA_ASSUME_NEW"
SKELETON_KEY = "bilingua"
PROFILE = "locos"  # the Bilingua compatibility profile: LocOS behaviour per format
# The wheel API this adapter needs (bilingua-py T1, 2026-11-07). An older wheel is "not
# installed" for the adapter: new imports stay Python.
# extract_handle (SPEED2, BILI-18): the adapter keeps the Document on the Rust side (DocumentHandle)
_REQUIRED_API = (
    "extract",
    "extract_handle",
    "merge",
    "locos_units",
    "locos_set_targets",
    "profiles",
    "version",
)
_LOG = logging.getLogger(__name__)

OPEN, CLOSE = "⟦", "⟧"  # the token delimiters of services/segment.py
PAIR_LETTER, POINT_LETTER, FREE_LETTER = "g", "x", "p"

# LocOS canonical format -> (Bilingua format ids, file name used to select the parser).
# Bilingua picks its parser by file name and content; LocOS only keeps bytes.
_FORMATS: dict[str, tuple[tuple[str, ...], str]] = {
    # wave 1 (BILI-17)
    "md": (("markdown",), "document.md"),
    "json": (("json",), "document.json"),
    "po": (("po",), "document.po"),
    # wave 2 (resource files, group A); vtt is a LocOS alias of srt
    "txt": (("txt",), "document.txt"),
    "srt": (("srt", "vtt"), "document.srt"),
    "strings": (("strings",), "document.strings"),
    "properties": (("properties",), "document.properties"),
    "ftl": (("ftl",), "document.ftl"),
    "dtd": (("dtd",), "document.dtd"),
    "resx": (("resx",), "document.resx"),
    "wxl": (("wxl",), "document.wxl"),
    "xaml": (("xaml",), "document.xaml"),
    "json5": (("json5",), "document.json5"),
    "arb": (("arb",), "document.arb"),
    "resjson": (("resjson",), "document.resjson"),
    "toml": (("toml",), "document.toml"),
    "ini": (("ini",), "document.ini"),
    "yaml": (("yaml",), "document.yaml"),
    "xcstrings": (("xcstrings",), "document.xcstrings"),
    "xcloc": (("xcloc",), "document.xcloc"),
    "js": (("js",), "document.js"),
    # wave 3 (bilingual and CAT); sdlrpx is LocOS's name for a Trados package document
    # (an .sdlppx upload, or the member blob services/sdl_package stores per target file)
    "xliff": (("xliff",), "document.xliff"),
    "sdlxliff": (("sdlxliff",), "document.sdlxliff"),
    "mqxliff": (("mqxliff",), "document.mqxliff"),
    "mxliff": (("mxliff",), "document.mxliff"),
    "txlf": (("txlf",), "document.txlf"),
    "txml": (("txml",), "document.txml"),
    "ttx": (("ttx",), "document.ttx"),
    "xlz": (("xlz",), "document.xlz"),
    "mqxlz": (("mqxlz",), "document.mqxlz"),
    "sdlrpx": (("sdlppx",), "document.sdlppx"),
    # wave 4 (Office and DTP); the template and macro extensions are LocOS aliases
    "docx": (("docx",), "document.docx"),
    "xlsx": (("xlsx",), "document.xlsx"),
    "pptx": (("pptx",), "document.pptx"),
    "odt": (("odt",), "document.odt"),
    "ods": (("ods",), "document.ods"),
    "odp": (("odp",), "document.odp"),
    "odg": (("odg",), "document.odg"),
    "vsdx": (("vsdx",), "document.vsdx"),
    "idml": (("idml",), "document.idml"),
    "icml": (("icml",), "document.icml"),
    "mif": (("mif",), "document.mif"),
    # wave 5 (HTML and XML family). The server-page dialects keep their own parser; xml is
    # LocOS's dispatching XML resource filter (Android strings, Java XML properties, ITS and
    # multilingual XML), which Bilingua's xml reader detects by content as LocOS does. svg is
    # not here: LocOS reads it with the image filter (canonical `image`, stays Python).
    "html": (("html",), "document.html"),
    "epub": (("epub",), "document.epub"),
    "eml": (("eml",), "document.eml"),
    "flprjzip": (("flprjzip",), "document.flprjzip"),
    "aspx": (("aspx",), "document.aspx"),
    "jsp": (("jsp",), "document.jsp"),
    "erb": (("erb",), "document.erb"),
    "php": (("php",), "document.php"),
    "phtml": (("phtml",), "document.phtml"),
    "shtml": (("shtml",), "document.shtml"),
    "mdx": (("mdx",), "document.mdx"),
    "hhc": (("hhc",), "document.hhc"),
    "hhk": (("hhk",), "document.hhk"),
    "hhp": (("hhp",), "document.hhp"),
    "xml": (("xml", "android", "javaxml"), "document.xml"),
    "dita": (("dita",), "document.dita"),
    "docbook": (("docbook",), "document.dbk"),
    "xslt": (("xslt",), "document.xsl"),
    "xmb": (("xmb",), "document.xmb"),
    "xtb": (("xtb",), "document.xtb"),
    "ttml": (("ttml",), "document.ttml"),
    # wave 6 (markup, subtitles, legacy and the remaining resource formats). LocOS's canonical
    # names are Bilingua's format ids. `ts` is the Qt Linguist XML filter: LocOS reads every
    # .ts upload with the js filter, which hands an XML file to it (see BilinguaFilter._handoff).
    "adoc": (("adoc",), "document.adoc"),
    "rst": (("rst",), "document.rst"),
    "wiki": (("wiki",), "document.wiki"),
    "dokuwiki": (("dokuwiki",), "document.dokuwiki"),
    "tex": (("tex",), "document.tex"),
    "ass": (("ass",), "document.ass"),
    "sbv": (("sbv",), "document.sbv"),
    "sub": (("sub",), "document.sub"),
    "scc": (("scc",), "document.scc"),
    "stl": (("stl",), "document.stl"),
    "stf": (("stf",), "document.stf"),
    "dxf": (("dxf",), "document.dxf"),
    "rc": (("rc",), "document.rc"),
    "desktop": (("desktop",), "document.desktop"),
    "catkeys": (("catkeys",), "document.catkeys"),
    "plist": (("plist",), "document.plist"),
    "stringsdict": (("stringsdict",), "document.stringsdict"),
    "mo": (("mo",), "document.mo"),
    "csv": (("csv",), "document.csv"),
    "tsv": (("tsv",), "document.tsv"),
    "xtg": (("xtg",), "document.xtg"),
    "ts": (("ts",), "document.ts"),
}
# Wave 6: the Qt Linguist switch name. LocOS routes the `.ts` extension to its js filter (a
# TypeScript module or, when the file is XML, the Qt Linguist filter), so `ts` is switched on
# its own but is reached through the js entry of the switch (`select`).
_QT_TS = "ts"
_VTT_NAME = "document.vtt"
# #672: the Bilingua file name for a .html page LocOS reads with a server-page filter
_HTML_SERVER_NAMES = {"phtml": "document.phtml", "jsp": "document.jsp", "aspx": "document.aspx"}
# JSON and ARB: LocOS reads strict UTF-8 (BOM allowed) and REFUSES anything else (P-B2).
# Bilingua's JSON readers fall back to a code page, so the adapter keeps this refusal.
# (json5 reads like every text filter and resjson takes a UTF-16 BOM: their decode gates.)
_STRICT_UTF8 = {"json", "arb"}
# Package formats: LocOS's zip guard (bombs, member counts) runs before Bilingua opens them,
# with the label the Python filter gives it, so a refusal reads the same ("This DOCX file has
# 6000 internal parts"). XLSX: xlsx_guard runs the zip guard itself (label "XLSX file").
_ZIP_LABELS = {
    "docx": "DOCX file",
    "pptx": "PowerPoint file",
    "xcloc": "Xcode localization catalog (.xcloc)",
    "odt": "OpenDocument file",
    "ods": "OpenDocument file",
    "odp": "OpenDocument file",
    "odg": "OpenDocument file",
    "vsdx": "Visio file",
    "idml": "IDML package",
    "epub": "EPUB file",
    "flprjzip": "Flare project",
}
# Wave 4: the Python filters' per-project "translate comments" option (#690, `comments=`) as
# the Bilingua option of each format, and the Python filter's default (DOCX translates them
# unless told not to; the others only when asked).
_COMMENT_OPTION = {
    "docx": ("docx.translate_comments", True),
    "xlsx": ("xlsx.translate_comments", False),
    "pptx": ("pptx.translate_comments", False),
    "odt": ("odf.translate_comments", False),
    "ods": ("odf.translate_comments", False),
    "odp": ("odf.translate_comments", False),
    "odg": ("odf.translate_comments", False),
    "vsdx": ("vsdx.translate_comments", False),
}
# The pair-token letter LocOS uses for this format's inline runs (oracle only; the wheel's
# to_locos_text applies the real rule).
_PAIR_LETTER = {"docx": "r", "pptx": "r", "xlsx": "r"}
# BilinguaError codes that mean the adapter handed the engine something wrong.
_ADAPTER_BUG_CODES = {"locked", "invented_tag", "duplicated_tag", "unknown_segment"}
_REIMPORT = "Re-import the file."
# Size, budget and nesting refusals come as code `limit` and decode refusals as `encoding`
# (wave 2 B side; since the W3 wheel also the json, docx, pptx and xlsx nesting caps, which
# were `parse` before): the adapter branches on the code, never on words in the message.


class BilinguaUnavailable(RuntimeError):
    """The `bilingua` module cannot be imported."""


class BilinguaParseError(ValueError):
    """Bilingua could not open or write the file (its error code is in `.code`; the parser's
    own sentence, without the engine's `not a <format> file:` prefix, in `.detail`)."""

    def __init__(self, message: str, code: str = "parse", detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail if detail is not None else message


class BilinguaParserMismatch(BilinguaParseError):
    """The stored skeleton was made by a parser this install cannot reproduce."""


class BilinguaEncodingError(BilinguaParseError, UnsupportedEncodingError):
    """An upload Bilingua could not read as text (code `encoding`). Also the LocOS #509
    refusal class (UnsupportedEncodingError), so every upload door shows the re-save
    sentence (422 on /api) as it does for the Python filters, never the generic 'corrupted'.
    Raised by `parse` only: at export the same code stays a BilinguaParseError (the stored
    original no longer reads: re-import)."""


class BilinguaModuleRefused(InputLimitError):
    """The LocOS refusal class of a file the format reads only in a safe subset (a js module
    that is not a static message export): the Python filter's InputLimitError, same text."""


class BilinguaWriteRefused(ValueError):
    """The translation cannot be written into this file (`unwritable`), or the adapter handed
    the engine a target it refused. Deliberately NOT a BilinguaParseError: export treats it
    as the Python filters' ValueError (a translation the translator must change), not as an
    unreadable blob."""

    def __init__(self, message: str, code: str = "unwritable"):
        super().__init__(message)
        self.code = code


# --- the switch ------------------------------------------------------------------------


def _module():
    """The `bilingua` module, or None when it is not importable or too old for the adapter."""
    try:
        import bilingua
    except Exception:
        return None
    if not all(hasattr(bilingua, name) for name in _REQUIRED_API):
        return None
    if not _knows_options(bilingua):
        return None
    return bilingua


# Format options the adapter hands the wheel that wheels before Bilingua 0.9.1 (with
# DocumentHandle) refuse as unknown (invalid_argument):
# such a wheel has the handle API but would refuse every .hhc/.hhk with a project code page and
# every .tex on an install with LATEX_TEXT_COMMANDS, so it counts as not installed too.
_OPTION_PROBES = (
    ("probe.hhc", b"<UL></UL>", {"htmlhelp.code_page": 1252, "htmlhelp.utf8_fallback": True}),
    ("probe.tex", b"x\n", {"tex.text_commands": "probe"}),
)
_KNOWS_OPTIONS: dict = {}


def _knows_options(bl) -> bool:
    """False when the wheel refuses one of _OPTION_PROBES' options as unknown; any other
    outcome (the probe file read or refused for its content) means the option is known.
    Asked once per module object."""
    key = id(bl)
    if key not in _KNOWS_OPTIONS:
        ok = True
        for name, data, opts in _OPTION_PROBES:
            try:
                bl.extract_handle(name, data, profile=PROFILE, format_options=opts)
            except Exception as exc:  # the probe's own content may be refused: only the code counts
                if getattr(exc, "code", None) == "invalid_argument" and "option" in str(exc):
                    ok = False
                    break
        _KNOWS_OPTIONS[key] = ok
    return _KNOWS_OPTIONS[key]


def available() -> bool:
    return _module() is not None


def enabled_formats() -> set[str]:
    """The canonical formats the operator switched to Bilingua: the stored
    `bilingua_formats` setting, which `LOC_TMS_BILINGUA_FORMATS` overrides (set but empty =
    off). Empty when unset. Never raises: a settings fault leaves every format on Python."""
    try:
        from app.services import settings

        env = os.environ.get(ENV_VAR)
        if env is not None:
            return set(settings.format_list(env))
        return set(settings.bilingua_formats())
    except Exception:
        return set()


def is_bilingua_skeleton(skeleton) -> bool:
    return isinstance(skeleton, dict) and isinstance(skeleton.get(SKELETON_KEY), dict)


def select(fmt: str, skeleton=None, new_import: bool = False):
    """The adapter for `fmt`, or None (the Python filter).

    `fmt` is the canonical format. The adapter is returned in two cases only: the stored
    skeleton carries the `bilingua` stamp (bound to it, even after the switch is turned
    off, because only the adapter can rebuild that document), or `new_import` is true (the
    first parse of an upload) and the format is switched on. A missing, empty or Python
    skeleton on any other call keeps the Python filter: an existing document is rebuilt by
    the filter that made it."""
    if fmt not in _FORMATS:
        return None
    if isinstance(skeleton, dict) and is_bilingua_skeleton(skeleton):
        if not available():
            raise BilinguaUnavailable(
                "This document was imported with Bilingua, which is not installed here."
            )
        return BilinguaFilter(fmt, skeleton)
    if skeleton:
        return None
    if (new_import or os.environ.get(ASSUME_NEW_VAR)) and available():
        on = enabled_formats()
        if fmt in on:
            return BilinguaFilter(fmt)
        if fmt == "js" and _QT_TS in on:
            # Qt Linguist on, TypeScript modules off: the adapter takes only the XML files
            return BilinguaFilter(fmt, modules=False)
    return None


def _trace(op: str, fmt: str) -> None:
    path = os.environ.get(TRACE_VAR)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()} {op} {fmt}\n")
        except OSError:
            pass


# --- the token oracle (tests only; the wheel's to_locos_text / from_locos_text are the rule)


_TOKEN_RE = re.compile(OPEN + r"(/)?([a-z])?(\d+)" + CLOSE)


def _pairs(inl: list[dict]) -> list:
    """Partner position of each Open/Close in a properly nested pair with the same id."""
    partner: list = [None] * len(inl)
    stack: list[tuple[str, int]] = []
    for pos, i in enumerate(inl):
        tag = i.get("tag")
        if not tag:
            continue
        if tag["kind"] == "open":
            stack.append((tag["id"], pos))
        elif tag["kind"] == "close":
            at = next((k for k in range(len(stack) - 1, -1, -1) if stack[k][0] == tag["id"]), None)
            if at is not None:
                o = stack[at][1]
                del stack[at:]
                partner[o] = pos
                partner[pos] = o
    return partner


def source_literals(inl: list[dict]) -> set[str]:
    """Token-shaped strings that are literal TEXT in the source inlines (not codes)."""
    return {m.group(0) for i in inl if "text" in i for m in _TOKEN_RE.finditer(i.get("text") or "")}


def inlines_to_locos(inl: list[dict], pair_letter: str = PAIR_LETTER) -> tuple[str, dict]:
    """Oracle of the positional rule (bilingua-model profile::tokens): Bilingua inlines to
    LocOS text and the side map {number-as-text: {id, letter, open, close, free, unpaired?}}.
    One token per tag, numbered from 1 in source order, skipping numbers that token
    look-alike text in the source uses; pairs are matched by id with proper nesting."""
    partner = _pairs(inl)
    taken = {int(m.group(3)) for m in map(_TOKEN_RE.match, source_literals(inl)) if m}
    text: list[str] = []
    cmap: dict = {}
    open_num: dict[int, int] = {}
    n = 0

    def nxt() -> int:
        nonlocal n
        n += 1
        while n in taken:
            n += 1
        return n

    for pos, i in enumerate(inl):
        tag = i.get("tag")
        if tag is None:
            text.append(i.get("text", ""))
            continue
        kind, display, tid = tag["kind"], tag.get("display", ""), tag["id"]
        free = bool(tag.get("free"))
        if kind == "open" and partner[pos] is not None:
            num = nxt()
            open_num[pos] = num
            cmap[str(num)] = {
                "id": tid,
                "letter": pair_letter,
                "open": display,
                "close": None,
                "free": False,
            }
            text.append(f"{OPEN}{pair_letter}{num}{CLOSE}")
        elif kind == "close" and partner[pos] is not None:
            num = open_num.get(partner[pos], 0)
            if str(num) in cmap:
                cmap[str(num)]["close"] = display
            text.append(f"{OPEN}/{pair_letter}{num}{CLOSE}")
        elif kind in ("open", "close"):
            num = nxt()
            cmap[str(num)] = {
                "id": tid,
                "letter": POINT_LETTER,
                "open": display if kind == "open" else None,
                "close": display if kind == "close" else None,
                "free": False,
                "unpaired": kind,
            }
            text.append(f"{OPEN}{POINT_LETTER}{num}{CLOSE}")
        else:
            num = nxt()
            letter = FREE_LETTER if free else POINT_LETTER
            cmap[str(num)] = {
                "id": tid,
                "letter": letter,
                "open": display,
                "close": None,
                "free": free,
            }
            text.append(f"{OPEN}{letter}{num}{CLOSE}")
    return "".join(text), cmap


def locos_to_inlines(text: str, cmap: dict) -> tuple[list[dict], list[dict]]:
    """Oracle of bilingua.from_locos_text: LocOS text back to inlines with the side map.
    Returns (inlines, problems). A malformed token, one naming no code in the map, or one of
    the wrong kind stays in the text as typed and is listed (#814); a code may be used more
    than once and a pair may be left open (the engine's set-target rules judge that)."""
    out: list[dict] = []
    problems: list[dict] = []
    lit: list[str] = []

    def flush():
        if lit:
            out.append({"text": "".join(lit)})
            lit.clear()

    pos = 0
    while pos < len(text):
        ch = text[pos]
        if ch == OPEN:
            m = _TOKEN_RE.match(text, pos)
            if m:
                tok = m.group(0)
                pos = m.end()
                tag, kind = _resolve(cmap.get(m.group(3)), bool(m.group(1)), m.group(2))
                if tag is None:
                    problems.append({"kind": kind, "token": tok})
                    lit.append(tok)
                else:
                    flush()
                    out.append({"tag": tag})
                continue
            problems.append({"kind": "stray", "at": pos})
        elif ch == CLOSE:
            problems.append({"kind": "stray", "at": pos})
        lit.append(ch)
        pos += 1
    flush()
    return out, problems


def _resolve(code: dict | None, closing: bool, letter) -> tuple[dict | None, str]:
    if code is None:
        return None, "unknown"
    point = code["letter"] in (POINT_LETTER, FREE_LETTER)
    if point:
        if closing or letter not in (None, POINT_LETTER, FREE_LETTER):
            return None, "kind_mismatch"
        unpaired = code.get("unpaired")
        if unpaired in ("open", "close"):
            return {"display": code.get(unpaired) or "", "id": code["id"], "kind": unpaired}, ""
        tag = {"display": code.get("open") or "", "id": code["id"], "kind": "standalone"}
        if code.get("free"):
            tag["free"] = True
        return tag, ""
    if letter in (POINT_LETTER, FREE_LETTER):
        return None, "kind_mismatch"  # any other letter (or none) names the pair, as in the wheel
    side = "close" if closing else "open"
    return {"display": code.get(side) or "", "id": code["id"], "kind": side}, ""


# --- errors ------------------------------------------------------------------------------


def _fail(exc, fmt: str = "") -> Exception:
    """One BilinguaError (anything with `.code`) as the LocOS refusal of plan 5.6."""
    code = getattr(exc, "code", None) or "error"
    msg = str(exc)
    # the parser's own sentence (wheels before W2 have no `.detail`: the message then)
    detail = getattr(exc, "detail", None) or msg
    if code == "limit":
        # B6 limits carry LocOS's numbers: the same plain refusal every door already maps;
        # a limit has no engine prefix, so detail and message are the same sentence
        return InputLimitError(_limit_text(detail))
    if code == "parser_version":
        return BilinguaParserMismatch(
            f"This document was imported with a Bilingua parser this install cannot reproduce "
            f"({msg}). {_REIMPORT}",
            code,
        )
    unsafe = _UNSAFE_RE.search(detail) if code in ("write", "unwritable") else None
    if unsafe:
        # the #676 export check (Bilingua's port of html_guard.check_build): LocOS's own
        # refusal class and sentence, which every export door shows
        from app.filters.html_guard import UnsafeMarkupRefused

        refused = UnsafeMarkupRefused(unsafe.group(0).strip())
        refused.code = code
        return refused
    if code in ("invalid_document", "write"):
        return BilinguaParseError(
            f"The stored file no longer matches what Bilingua imported ({code}): {msg}. "
            f"{_REIMPORT}",
            code,
        )
    if code == "unwritable":
        return BilinguaWriteRefused(f"The translation cannot be written into this file: {msg}")
    if code in _ADAPTER_BUG_CODES:
        _LOG.error("bilingua adapter bug: %s refused a target (%s): %s", fmt, code, msg)
        return BilinguaWriteRefused(
            f"The translated file could not be written ({code}): {msg}. This has been logged.",
            code,
        )
    if code == "internal":
        # by format only: an engine message can quote the file (the panic rule of ingest)
        _LOG.error("bilingua internal error on a %s file (message withheld)", fmt)
        return BilinguaParseError(
            "The parser failed on this file; it has been logged. Contact support with the "
            "document ID.",
            code,
        )
    if code == "encoding":
        # the Python filters' sentence ("...; re-save the file as UTF-8."), which every door
        # and suite already shows (#509): no engine prefix
        text = re.sub(r"\.?\s*Re-save the file as UTF-8", "; re-save the file as UTF-8", detail)
        text = re.sub(r"^This file is not valid UTF-8", "file is not valid UTF-8", text)
        return BilinguaParseError(text[:1].lower() + text[1:], code)
    return BilinguaParseError(
        f"Bilingua could not read or write this file ({code}): {msg}", code, detail
    )


# The #676 export refusal sentence ("This page cannot be exported safely: in the translated
# file a web browser would read ..."), as Bilingua's HTML-family writers word it.
_UNSAFE_RE = re.compile(r"This (?:[\w.-]+ ){0,3}cannot be exported safely:.*", re.S)


def _coded(exc: Exception, default: str) -> Exception:
    """Give a LocOS refusal raised on the adapter's behalf (a Python gate or guard) the engine
    code of its class, as every adapter refusal has one (plan 5.6)."""
    if getattr(exc, "code", None) is None:
        if isinstance(exc, UnsupportedEncodingError):
            exc.code = "encoding"
        elif isinstance(exc, InputLimitError) and default != "write":
            exc.code = "limit"
        else:
            exc.code = default
    return exc


def _limit_text(msg: str) -> str:
    """Bilingua's refusal in LocOS's words: the same sentences, naming the setting to raise."""
    msg = re.sub(r"segments\. Split", "segments (MAX_DOCUMENT_SEGMENTS). Split", msg)
    msg = re.sub(r"characters\. Break", "characters (MAX_SEGMENT_CHARS). Break", msg)
    msg = re.sub(r"lines\. Split", "lines (20 x MAX_DOCUMENT_SEGMENTS). Split", msg)
    if "MAX_" not in msg:
        # the pre-parse budgets ("far more strings than the limit of 3 segments allows",
        # "than 50000 segments allow", "the 50000 unit limit"): LocOS names the setting
        msg = re.sub(
            r"(\d[\d,]* (?:segments?|units?)(?: allows?| limit(?: allows)?)?)",
            r"\1 (MAX_DOCUMENT_SEGMENTS)",
            msg,
            count=1,
        )
    return msg


def _call(bl, fmt: str, fn, *args, **kwargs):
    """Call the engine; every failure comes back as a LocOS refusal (never a raw panic)."""
    try:
        return fn(*args, **kwargs)
    except bl.BilinguaError as exc:
        raise _fail(exc, fmt) from exc
    except BaseException as exc:
        if type(exc).__name__ != "PanicException":
            raise
        _LOG.error("bilingua panicked on a %s file (message withheld)", fmt)
        raise BilinguaParseError(
            "The parser failed on this file; it has been logged. Contact support with the "
            "document ID.",
            "internal",
        ) from None


# --- helpers -----------------------------------------------------------------------------


def _sha(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _units_sha(units: list[dict]) -> str:
    """The units hash of the stamp: p_index, segment id and token text, in order."""
    return _sha([[u["p_index"], u["segment_id"], u["text"]] for u in units])


def profile_stamp(bl=None) -> dict:
    """The current version of the compatibility profile this install applies: {name, version}."""
    bl = bl or _module()
    if bl is None:
        return {"name": None, "version": 0}
    return {"name": PROFILE, "version": int((bl.profiles() or {}).get(PROFILE, 0))}


def limits_from_config() -> dict:
    """LocOS's input and archive budgets as Bilingua's B6 Limits (the same numbers the
    Python filters apply, from config.py)."""
    return {
        "max_input_bytes": config.MAX_UPLOAD_BYTES,
        "max_units": config.MAX_DOCUMENT_SEGMENTS,
        "max_unit_chars": config.MAX_SEGMENT_CHARS,
        "max_lines": 20 * config.MAX_DOCUMENT_SEGMENTS,
        "max_zip_entries": config.ZIP_MAX_MEMBERS,
        "max_zip_bytes": config.ZIP_MAX_UNCOMPRESSED_BYTES,
        "max_zip_ratio": config.ZIP_MAX_RATIO,
        "zip_ratio_floor_bytes": config.ZIP_RATIO_MIN_BYTES,
        "max_paragraphs": config.DOCX_MAX_PARAGRAPHS,
        "xlsx_max_cells": config.XLSX_MAX_CELLS,
        "xlsx_max_rows": config.XLSX_MAX_ROWS,
        "xlsx_max_cell_chars": config.XLSX_MAX_CELL_CHARS,
        "xlsx_max_total_text_chars": config.XLSX_MAX_TOTAL_TEXT_CHARS,
    }


def _default_walk(fmt: str):
    """The walk version a document without a stamp gets (what export passes for a
    skeleton the adapter made, which carries no LocOS walk stamp)."""
    try:
        if fmt == "md":
            from app.filters.markdown_filter import MD_WALK_VERSION

            return MD_WALK_VERSION
        if fmt == "docx":
            from app.filters.docx_filter import DOCX_WALK_VERSION

            return DOCX_WALK_VERSION
        if fmt == "xlsx":
            from app.filters.xlsx_filter import XLSX_WALK_VERSION

            return XLSX_WALK_VERSION
        if fmt == "pptx":
            from app.filters.pptx_filter import PPTX_WALK_VERSION

            return PPTX_WALK_VERSION
    except ImportError:
        return None
    return None


_CSV_DELIMITERS = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}


def _csv_plan_columns(table) -> str | None:
    """A Python CSV/TSV column plan (skeleton["table"], what the Python tabular filter's
    build takes as `table=`) as the Bilingua `csv.columns` mapping with 1-based column
    numbers: the same source and target columns, header row and delimiter. None for an
    in-place plan (no target column: both engines plan it again the same way) or no plan."""
    plan = table.get("plan") if isinstance(table, dict) else None
    if not isinstance(plan, dict) or plan.get("inplace") or plan.get("source") is None:
        return None
    parts = [f"source={int(plan['source']) + 1}"]
    for loc, col in sorted((plan.get("targets") or {}).items()):
        parts.append(f"target.{loc}={int(col) + 1}")
    parts.append("header=" + ("yes" if plan.get("header") else "no"))
    delim = _CSV_DELIMITERS.get(str(table.get("delimiter") or ""))
    if delim:
        parts.append("delimiter=" + delim)
    return ", ".join(parts)


def options_from_kwargs(fmt: str, kwargs: dict) -> dict:
    """The LocOS parse/export keywords that change output, as Bilingua per-format options
    (flat keys "<format>.<option>", values str, bool or int). A keyword at its default is
    not an option: a skeleton the adapter made carries no LocOS walk or dialect stamp, so
    export hands the defaults, which mean nothing to Bilingua. Parse keywords (wave 4):
    `comments` (#690) as `<format>.translate_comments` when it is not the Python filter's
    default, and XLSX column mode (`columns`, `target_locales`) as `xlsx.columns` and
    `xlsx.target_locales`; both are stamped and replayed on rebuild."""
    opts: dict = {}
    walk = kwargs.get("walk")
    if walk is not None and fmt in ("docx", "xlsx", "pptx") and walk != _default_walk(fmt):
        opts[f"{fmt}.walk"] = str(walk)
    if kwargs.get("dialect") not in (None, "plain") and fmt == "json":
        opts["json.dialect"] = str(kwargs["dialect"])
    md_walk = kwargs.get("md_walk")
    if md_walk is not None and fmt == "md" and md_walk != _default_walk("md"):
        opts["markdown.walk"] = str(md_walk)
    if kwargs.get("fm_keys") is not None and fmt == "md":  # an empty allowlist is a list too
        opts["markdown.fm_keys"] = ",".join(sorted(kwargs["fm_keys"]))
    if kwargs.get("fm_keys") is not None and fmt == "mdx":
        opts["mdx.front_matter_keys"] = ",".join(sorted(kwargs["fm_keys"]))
    cp = kwargs.get("hhp_code_page")
    if fmt in ("hhc", "hhk") and isinstance(cp, int) and not isinstance(cp, bool):
        # decision 21: the code page of the help project's Language= (ingest's hhp_code_page,
        # export's stamped value), for a file that declares no charset; a .hhp reads its own
        opts["htmlhelp.code_page"] = cp
    comment = _COMMENT_OPTION.get(fmt)
    if comment and kwargs.get("comments") is not None and bool(kwargs["comments"]) != comment[1]:
        opts[comment[0]] = bool(kwargs["comments"])
    family = {"xlsx": "xlsx", "csv": "csv", "tsv": "csv"}.get(fmt)
    if family == "csv" and not kwargs.get("columns"):
        # CSV/TSV without a mapping still plan by the header row, which may name one of the
        # project's target languages (ingest always hands them to the tabular filter)
        locales = [str(t) for t in kwargs.get("target_locales") or () if t]
        if locales:
            opts["csv.target_locales"] = ",".join(locales)
    elif family and kwargs.get("columns"):
        # column mode (Batch 6; CSV and TSV share the `csv.` options): the user's mapping and
        # the project's languages for header matching; a stored plan (a dict, the Python
        # stamp) never reaches a Bilingua import
        cols = kwargs["columns"]
        if not isinstance(cols, str):
            raise BilinguaParseError(
                "A stored column plan cannot be replayed by Bilingua; re-import the file.",
                "invalid_argument",
            )
        opts[f"{family}.columns"] = cols
        locales = [str(t) for t in kwargs.get("target_locales") or () if t]
        if locales:
            opts[f"{family}.target_locales"] = ",".join(locales)
    return opts


# Unit extras of Bilingua's LocOS profile: the image scan LocOS records as skeleton["a11y_images"]
# (markdown_filter.parse): images per anchor unit, and the file names of those with an empty alt.
# Since the W5 B wheel, docx, html, the server pages and mdx also give every finding of both kinds
# (`missing`: no alt at all, `empty`) as a JSON list [{"kind", "where"}] in file order.
_A11Y_IMAGES = "locos.a11y_images"
_A11Y_EMPTY_ALT = "locos.a11y_empty_alt"
_A11Y_MISSING_ALT = "locos.a11y_missing_alt"
_A11Y_KINDS = ("missing", "empty")


def _extras(doc, names: list[str]) -> list[dict]:
    """`context.extra` of the units that carry one of `names`, in file order, as the handle's
    `unit_extras` gives it: [{"unit", "segments" (segment ids), "translate", "extra"}].
    `doc` is a DocumentHandle (SPEED2); nothing is converted to a dict."""
    return [e for e in doc.unit_extras(list(names)) if any(n in e["extra"] for n in names)]


def a11y_images(doc, locos: list[dict]) -> dict | None:
    """skeleton["a11y_images"] as the Python filter writes it, from the image inventory the
    wheel puts on each anchor unit (count, findings); None with no image. The findings are
    `locos.a11y_missing_alt` (both kinds, file order) when the unit has it, else the
    empty-alt file names of `locos.a11y_empty_alt` (Markdown, older wheels).
    `doc` is the DocumentHandle."""
    from app.filters import a11y

    seg_p = {u["segment_id"]: u["p_index"] for u in locos}
    images = 0
    missing: list[dict] = []
    for entry in _extras(doc, [_A11Y_IMAGES, _A11Y_EMPTY_ALT, _A11Y_MISSING_ALT]):
        extra = entry["extra"]
        try:
            n = int(extra.get(_A11Y_IMAGES) or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            continue
        segs = entry["segments"] or []
        anchor = seg_p.get(segs[0]) if segs else None
        images += n
        found = _a11y_findings(extra.get(_A11Y_MISSING_ALT))
        if found is not None:
            missing.extend(a11y.missing_alt(kind, anchor, where) for kind, where in found)
            continue
        empty = extra.get(_A11Y_EMPTY_ALT)
        if isinstance(empty, str):
            missing.extend(a11y.missing_alt("empty", anchor, w) for w in empty.split("\n"))
    return {"images": images, "missing_alt": missing} if images else None


def _a11y_findings(raw) -> list[tuple[str, str]] | None:
    """The (kind, where) pairs of a `locos.a11y_missing_alt` value, or None when the unit has
    none or it is not the wheel's JSON list (the empty-alt names are read instead). An entry
    of an unknown kind is skipped: the prompt text exists for `missing` and `empty` only."""
    if not isinstance(raw, str):
        return None
    try:
        items = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    out: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("kind") not in _A11Y_KINDS:
            continue
        where = item.get("where")
        out.append((item["kind"], where if isinstance(where, str) else ""))
    return out


def _unit_notes(doc: dict) -> dict[str, str]:
    """{segment id: note} from the units' `context.notes`, joined as LocOS's tabular
    unit_note joins the note columns ("Key: a | notes: button")."""
    out: dict[str, str] = {}
    for unit in doc.get("units") or []:
        notes = [str(n) for n in (unit.get("context") or {}).get("notes") or [] if str(n).strip()]
        segs = unit.get("segments") or []
        if notes and segs:
            out[str(segs[0].get("id"))] = " | ".join(notes)
    return out


def _notes_of(doc) -> dict[str, str]:
    """`_unit_notes` for a DocumentHandle: the handle's `unit_notes()` when the wheel has it
    (no Document conversion; FINAL3 I1), the to_dict path for an older wheel."""
    fn = getattr(doc, "unit_notes", None)
    if fn is None:
        return _unit_notes(doc.to_dict())
    out: dict[str, str] = {}
    for e in fn():
        notes = [str(n) for n in e.get("notes") or [] if str(n).strip()]
        segs = e.get("segments") or []
        if notes and segs:
            seg = segs[0]
            out[str(seg.get("id") if isinstance(seg, dict) else seg)] = " | ".join(notes)
    return out


# Wave 5: formats whose Python filter records a note per unit (skeleton["units"][i]["note"]).
_UNIT_NOTES = {"xml", "dita", "docbook", "xmb", "xtb", "csv", "tsv"}


_JSON_DIALECTS = ("chrome", "formatjs", "structured", "go", "arb")


def _json_dialect(doc) -> str | None:
    """The resource dialect Bilingua read a JSON file as (its units' `json.dialect` extra),
    or None for generic JSON. `doc` is the DocumentHandle."""
    for entry in _extras(doc, ["json.dialect"]):
        name = entry["extra"].get("json.dialect")
        if name in _JSON_DIALECTS:
            return name
    return None


def _md_fm_keys(options: dict) -> list[str]:
    """The front matter allowlist an md (or mdx) import used: `markdown.fm_keys` /
    `mdx.front_matter_keys` when set (split as front_matter_keys splits the env var), else the
    default list; sorted and lower-cased."""
    from app.filters.markdown_filter import DEFAULT_FRONT_MATTER_KEYS

    raw = options.get("markdown.fm_keys", options.get("mdx.front_matter_keys"))
    names = DEFAULT_FRONT_MATTER_KEYS if raw is None else str(raw).split(",")
    return sorted({n.strip().lower() for n in names if n.strip()})


def install_options(fmt: str) -> dict:
    """Host settings the Python filter reads at parse time, as Bilingua options for a NEW
    import (stamped, then replayed from the stamp on rebuild): the markdown front matter
    allowlist `LOC_TMS_FRONT_MATTER_KEYS` (#731) when the host changed it from the default,
    Fluent terms `FTL_TRANSLATE_TERMS`, XAML view attributes `XAML_VIEW_ATTRIBUTES`, LaTeX
    inline commands `LATEX_TEXT_COMMANDS`."""
    if fmt == "stf":
        # LocOS lists an STF file's units with no targets (a Bilingual file's TRANSLATION
        # cells are not loaded as segment targets)
        return {"stf.load_targets": False}
    if fmt == "tex":
        # LATEX_TEXT_COMMANDS (latex_filter._extra_inline): the install's extra inline text
        # commands, the same names the Python filter accepts, in the wheel's comma spelling
        from app.filters.latex_filter import _extra_inline

        names = list(_extra_inline())
        return {"tex.text_commands": ",".join(names)} if names else {}
    if fmt == "wiki":
        # WIKI_TEMPLATE_PARAMS (wiki_filter): the install's translatable template parameters,
        # in the same `Template:param` spelling
        raw = (getattr(config, "WIKI_TEMPLATE_PARAMS", "") or "").strip()
        return {"wiki.template_params": raw} if raw else {}
    if fmt == "ftl":
        from app.filters.fluent_filter import _translate_terms

        return {"ftl.translate_terms": True} if _translate_terms() else {}
    if fmt == "xaml":
        from app.filters.xaml_filter import _view_attrs_enabled

        return {"xaml.view_attributes": True} if _view_attrs_enabled() else {}
    if fmt == "mdx":
        # the JSX attribute allowlist LOC_TMS_MDX_ATTRS (mdx_filter.translatable_attrs) and the
        # front matter allowlist, each only when the host changed it
        from app.filters.markdown_filter import DEFAULT_FRONT_MATTER_KEYS, front_matter_keys
        from app.filters.mdx_filter import DEFAULT_ATTRS, translatable_attrs

        opts: dict = {}
        attrs = translatable_attrs()
        if attrs != frozenset(DEFAULT_ATTRS):
            opts["mdx.attrs"] = ",".join(sorted(attrs))
        keys = front_matter_keys()
        if os.environ.get("LOC_TMS_FRONT_MATTER_KEYS") is not None and keys != frozenset(
            k.lower() for k in DEFAULT_FRONT_MATTER_KEYS
        ):
            opts["mdx.front_matter_keys"] = ",".join(sorted(keys))
        return opts
    if fmt != "md" or os.environ.get("LOC_TMS_FRONT_MATTER_KEYS") is None:
        return {}
    from app.filters.markdown_filter import DEFAULT_FRONT_MATTER_KEYS, front_matter_keys

    keys = front_matter_keys()
    if keys == frozenset(k.lower() for k in DEFAULT_FRONT_MATTER_KEYS):
        return {}
    return {"markdown.fm_keys": ",".join(sorted(keys))}


def _code_page_hint():
    """The decision 22 hint ingest set around this parse (base.source_code_page), or None."""
    from app.filters import base

    var = getattr(base, "_CODE_PAGE_HINT", None)
    return var.get() if var is not None else None


# Bilingua names encodings by the WHATWG Encoding Standard, whose CJK decoders are the
# Windows code pages LocOS reads (decision 22, app/filters/code_pages.py): WHATWG Shift_JIS is
# Windows-31J (cp932), EUC-KR is Windows-949, Big5 is read as cp950 by LocOS. Python's codec
# names for those labels are the narrower ISO tables, so they are mapped here first.
_WHATWG_LOCOS = {
    "shift_jis": "cp932",
    "shift-jis": "cp932",
    "windows-31j": "cp932",
    "euc-kr": "cp949",
    "big5": "cp950",
    "windows-874": "cp874",
}


def _codec(label) -> str | None:
    """A Bilingua encoding label (WHATWG names: "windows-1251", "Shift_JIS", "ISO-8859-8",
    "UTF-16LE") as the codec name LocOS stamps and shows ("cp1251", "shift_jis",
    "iso8859-8", "utf-16-le"), by the same lookup base.read_text_upload uses."""
    if not isinstance(label, str) or not label.strip():
        return None
    from app.filters.base import _codec_name

    whatwg = _WHATWG_LOCOS.get(label.strip().lower())
    if whatwg:
        return whatwg
    name = _codec_name(label.strip())
    if name:
        return name
    try:
        return codecs.lookup(label.strip()).name
    except LookupError:
        return label.strip().lower()


def _put_warnings(skeleton: dict, warnings: list, hint) -> None:
    """skeleton["file_warnings"]: the parse's own warnings, then the decision 22 hint's
    encoding warnings as base.apply_code_page_hint puts them (replacing earlier ones)."""
    from app.filters.base import TEXT_ENCODING_WARNING

    out = list(warnings)
    for w in getattr(hint, "warnings", None) or []:
        if isinstance(w, dict) and w.get("kind") == TEXT_ENCODING_WARNING and w not in out:
            out.append(dict(w))
    if out:
        skeleton["file_warnings"] = out


# Encodings that are one code page under two names (WHATWG reads Latin-1 as Windows-1252, and
# the Windows DBCS pages are the WHATWG Shift_JIS / GBK / EUC-KR / Big5 decoders).
_SAME_PAGE = {
    "latin_1": "cp1252",
    "iso8859-1": "cp1252",
    "iso8859-9": "cp1254",
    "iso8859-11": "cp874",
    "tis-620": "cp874",
    "shift_jis": "cp932",
    "gb2312": "gbk",
    "cp936": "gbk",
    "euc_kr": "cp949",
    "big5": "cp950",
    "utf-8-sig": "utf-8",
}


def _page(label) -> str | None:
    name = _codec(label)
    if not name:
        return None
    with contextlib.suppress(LookupError):
        name = codecs.lookup(name).name
    return _SAME_PAGE.get(name, name)


def _other_code_page(label: str | None, bilingua_encoding) -> bool:
    """True when LocOS's upload gate read a legacy (non-Unicode) file in one code page and
    Bilingua read it in another: the text the two give differs (decision 22)."""
    ours, theirs = _page(label), _page(bilingua_encoding)
    if not ours or not theirs or ours.startswith("utf"):
        return False
    return ours != theirs


def _hint_code_page(enc: str | None) -> str | None:
    """The `text_code_page` stamp for an encoding: a decision 22 code page other than
    Windows-1252 (the codecs `document_code_page` accepts), else None."""
    from app.filters.base import _HINT_CODECS

    if enc and enc != "cp1252" and enc in _HINT_CODECS:
        return enc
    return None


def _fallback_encoding(data: bytes, source_lang: str | None) -> str | None:
    """The upload's encoding for display when Bilingua does not report one (its binary
    skeletons, md/po/srt, have no `source_encoding` yet): UTF-8 when it decodes, else the
    LocOS #509 reading (run under its own hint, so the ingest hint gets no warning)."""
    try:
        bytes(data).decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    from app.filters.base import UnsupportedEncodingError, read_text_upload, source_code_page

    try:
        with source_code_page(source_lang or None):
            return _codec(read_text_upload(bytes(data), sig=True, declared=True)[1])
    except (UnsupportedEncodingError, ValueError):
        return None


def _gate_text(sig: bool, declared: bool = False):
    def gate(data: bytes) -> str:
        from app.filters.base import read_text_upload

        return read_text_upload(data, sig=sig, declared=declared)[1]

    return gate


def _gate_resource(fmt: str):
    def gate(data: bytes) -> str:
        return _python_filter(fmt)._decode(data)[1].label

    return gate


def _gate_module(module: str, pos: int | None):
    def gate(data: bytes) -> str:
        import importlib

        out = importlib.import_module(f"app.filters.{module}")._decode(data)
        return "utf-8" if pos is None else out[pos]

    return gate


# Wave 3 (bilingual and CAT). The Python filters of these formats record no `source_encoding`
# and take no decision 22 hint: their decode step only refuses (InputLimitError, LocOS's own
# sentence) what the byte-splice build could not write back. The gates below run that step,
# and for the package formats the Python archive checks (zip_guard with the package's own
# label, member names and counts), so a refusal has the Python class and sentence. Each
# returns "utf-8": nothing is stamped.


def _xliff_member(raw: bytes, what: str = "This XLIFF file") -> None:
    """The cat_xliff parse_units entry checks: the decode step, then the #571 pre-parse
    markup budget (byte counts, before any tree is built)."""
    from app.filters import cat_xliff, xliff_filter

    cat_xliff._decode(raw)
    xliff_filter.check_markup_budget(raw, what)


def _gate_cat(data: bytes) -> str:
    _xliff_member(data)
    return "utf-8"


def _gate_txml(data: bytes) -> str:
    from app.filters import wordfast, xliff_filter

    wordfast._decode_txml(data)
    xliff_filter.check_markup_budget(data, "This TXML file")
    return "utf-8"


def _gate_xliff(data: bytes) -> str:
    """Plain XLIFF: the markup budget first (xliff_filter._parse_xml capped), then the
    encoding. It reads any encoding lxml reads, EXCEPT a file with <seg-source>, which the
    segmented (cat_xliff) path splices and so refuses a non-UTF encoding. Only when the cheap
    cat_xliff check refuses does the Python filter parse the file, to decide which path."""
    from app.filters import cat_xliff, xliff_filter

    xliff_filter.check_markup_budget(data)
    try:
        cat_xliff._decode(data)
    except InputLimitError:
        _python_filter("xliff").parse(data)  # raises LocOS's refusal when LocOS refuses
    return "utf-8"


def _gate_ttx(data: bytes) -> str:
    from app.filters import ttx_filter

    text, _codec, _bom = ttx_filter._decode(data)
    ttx_filter._budget(text)
    return "utf-8"


def _gate_xliff_package(fmt: str):
    def gate(data: bytes) -> str:
        from app.filters import xliff_package

        with xliff_package.open_zip(data, fmt) as zf:
            for _i, info in xliff_package._members(zf, fmt):
                _xliff_member(zf.read(info))
        return "utf-8"

    return gate


def _gate_sdl_package(data: bytes) -> str:
    """The Trados package document checks (a member blob, or a raw package with exactly one
    target file): sdl_package._open_member, then the member's decode step."""
    from app.filters import sdl_package

    zf, _meta, _proj, member = sdl_package._open_member(data)
    with zf:
        _xliff_member(zf.read(member))
    return "utf-8"


# Wave 4 (Office and DTP). The Python filters record no `source_encoding` (MIF reads UTF-8 only)
# and take no decision 22 hint. These gates run the cheap pre-parse checks of the Python filter
# that refuse before any tree is built (check_input already ran the zip guard with the Python
# label): the DTD rule (#814.4), the #571 markup budgets, the OpenDocument package checks, the
# Visio page check, the InCopy DOCTYPE and tag budgets, MIF's version, UTF-8 and statement
# budget. Every other refusal is decided by the Python filter itself (see _REFUSAL_BY_PYTHON).


def _gate_docx(data: bytes) -> str:
    from app.filters import docx_filter, zip_guard

    zip_guard.refuse_doctype(data, "DOCX file")
    # not a package: Bilingua refuses it below (the Python filter would crash here)
    with contextlib.suppress(zipfile.BadZipFile):
        docx_filter._check_markup_budget(data)
    return "utf-8"


def _gate_pptx(data: bytes) -> str:
    from app.filters import pptx_filter

    with contextlib.suppress(zipfile.BadZipFile):  # not a package: see _gate_docx
        pptx_filter.check_markup_budget(data)
    return "utf-8"


def _gate_odf(data: bytes) -> str:
    from app.filters import odf_filter

    with odf_filter._open_package(data) as zf:
        names = set(zf.namelist())
        for part in odf_filter._PARTS:
            if part in names:
                odf_filter.check_odf_budget(zf.read(part))
    return "utf-8"


def _gate_vsdx(data: bytes) -> str:
    import io
    import zipfile

    from app.filters import vsdx_filter as vf

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InputLimitError("This Visio file is not a valid .vsdx/.vsdm package.") from exc
    with zf:
        names = set(zf.namelist())
        if vf._PAGES not in names and not any(vf._PAGE_PART_RE.match(n) for n in names):
            raise InputLimitError("This Visio file has no drawing pages (visio/pages/).")
        vf._check_markup_budget(
            zf, [n for n in zf.namelist() if n.startswith("visio/pages/") and n.endswith(".xml")]
        )
    return "utf-8"


def _gate_icml(data: bytes) -> str:
    from app.filters import icml_filter
    from app.filters.xml_guard import check_tag_budget

    if icml_filter._DOCTYPE_RE.search(data):
        raise icml_filter.IcmlError(
            "This InCopy file declares a DOCTYPE or entity; ICML never needs one."
        )
    if data.count(b"<") > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            "This InCopy file has far more markup than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS} segments). Split the document."
        )
    check_tag_budget(data, what="This InCopy file")
    return "utf-8"


def _gate_mif(data: bytes) -> str:
    from app.filters import mif_filter

    mif_filter._check_version(bytes(data[:8192]))
    text, _bom = mif_filter._decode(data)
    budget = mif_filter._STATEMENTS_PER_SEGMENT * config.MAX_DOCUMENT_SEGMENTS
    if text.count("<") > budget:
        raise InputLimitError(
            f"This MIF file has more than {budget} statements "
            f"({mif_filter._STATEMENTS_PER_SEGMENT} x MAX_DOCUMENT_SEGMENTS). Split it into "
            "smaller files."
        )
    return "utf-8"


def _gate_none(data: bytes) -> str:
    return "utf-8"


# Wave 5 (HTML and XML family). Each gate runs the Python filter's own upload checks that
# refuse before (or instead of) a unit walk, so every refusal LocOS makes on an upload is made
# here with its class and sentence: #509 decode (declared and rare charsets), the #571 and #792
# markup budgets, the DTD internal subset / entity / DOCTYPE refusals (XXE), well-formedness,
# the server-page safety refusals (code used as a tag or attribute name, code printing half a
# tag, reserved characters, PHP mixed with <% %>), the PHP router's code-only refusal, the
# EPUB and Flare package checks (DRM, unsafe paths, symlinks, mimetype), and the e-mail MIME
# limits and signed-message refusal. They return the label the Python filter records as
# `source_encoding` (read_text_upload also sets the decision 22 hint as the Python filter
# does). Units still come from Bilingua.


def _html_budget(text: str, what: str = "HTML") -> None:
    if text.count("<") > 10 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"This {what} file has far more tags than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into smaller files."
        )


def _server_checks(sp, text: str) -> None:
    """The server-page filter's upload refusals (server_page_filter._ServerPageFilter.parse):
    the tag count, then masking the code islands and walking the masked page, which refuses
    code used as a tag or attribute name and the #571 tag-scan budget."""
    _html_budget(text, sp.label)
    masked, codes, inline = sp._mask(text)
    sp._tokens(masked, codes, inline, [100 * config.MAX_DOCUMENT_SEGMENTS])


def _html_server(text: str):
    """The server-page filter LocOS uses for a .html page that holds closed server code
    (#672, server_page_filter.server_page_for_html), else None. Refuses a page mixing PHP
    and <% %> code."""
    from app.filters.server_page_filter import server_page_for_html

    return server_page_for_html(text)


def _gate_html(data: bytes) -> str:
    from app.filters.base import read_text_upload

    text, enc = read_text_upload(data, declared=True, rare=True)
    server = _html_server(text)
    if server is not None:
        _server_checks(server, text)
    else:
        _html_budget(text)
    return enc


def _gate_server(fmt: str):
    def gate(data: bytes) -> str:
        from app.filters.base import read_text_upload

        text, enc = read_text_upload(data, declared=True, rare=True)
        _server_checks(_python_filter(fmt), text)
        return enc

    return gate


def _gate_php(data: bytes) -> str:
    """The .php router (server_page_filter.PhpFilter._pick): a language array goes to the
    array filter (its refusals: _REFUSAL_BY_PYTHON), code with no page text is refused, a
    template gets the server-page checks."""
    from app.filters.base import read_text_upload

    picked = _python_filter("php")._pick(data)
    if not hasattr(picked, "_mask"):
        return "utf-8"
    text, enc = read_text_upload(data, declared=True, rare=True)
    _server_checks(picked, text)
    return enc


def _gate_epub(data: bytes) -> str:
    """epub_filter._Package (zip guard, container, mimetype, OPF, DRM) and the chapter and
    NCX checks EpubFilter.parse makes (HTML decode and tag budget, the NCX XML gate)."""
    from app.filters import epub_filter
    from app.filters.base import read_text_upload

    pkg = epub_filter._Package(bytes(data))
    for path in pkg.xhtml + ([pkg.nav] if pkg.nav else []):
        text, _enc = read_text_upload(pkg._read(path), declared=True, rare=True)
        _html_budget(text)
    if pkg.ncx:
        epub_filter._parse_xml(pkg._read(pkg.ncx), "its NCX table of contents")
    return "utf-8"


def _gate_flare(data: bytes) -> str:
    """flare_filter: zip guard, member names (paths, duplicates, symlinks), the Flare
    project test, per file the UTF-8 decode and XML gate, and the markup budget."""
    from app.filters import flare_filter as ff

    zf = ff._open(bytes(data))
    with zf:
        members = ff._members(zf)
        if not ff._is_flare(zf, members):
            raise InputLimitError(
                "This zip is not a MadCap Flare project: it has no .flprj project file, "
                "no Flare snippets, variables, TOCs or glossaries, and no topics with the "
                "MadCap namespace. It was not processed."
            )
        markup = 0
        for info, kind in members:
            if kind is None:
                continue
            text, _bom = ff._decode(zf.read(info), info.filename)
            ff._check_xml(text, info.filename, structured=kind != "topic")
            markup += text.count("<")
            if markup > 10 * config.MAX_DOCUMENT_SEGMENTS:
                raise InputLimitError(
                    "This Flare project has far more markup than MAX_DOCUMENT_SEGMENTS allows "
                    f"({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into smaller ones."
                )
    return "utf-8"


def _gate_eml(data: bytes) -> str:
    """email_filter._walk: not an e-mail, header and MIME depth limits, damaged or binary
    text parts, signed or encrypted messages."""
    from app.filters import email_filter

    email_filter._walk(bytes(data))
    return "utf-8"


def _gate_mdx(data: bytes) -> str:
    from app.filters.base import read_text_upload

    return read_text_upload(data)[1]  # #509 decode + #571 line budget


def _gate_htmlhelp(data: bytes, kwargs: dict | None = None) -> str:
    """htmlhelp_filter._load: the byte and line budgets and the HTML Help decode (BOM, binary,
    declared charset, then the project code page `hhp_code_page` for .hhc/.hhk or the
    project's own Language= for .hhp, then Windows-1252)."""
    from app.filters import htmlhelp_filter as hh

    raw = bytes(data)
    kwargs = kwargs or {}
    version = hh.decode_version(kwargs.get("hh_decode", hh.HH_DECODE_VERSION))
    if kwargs.get("_fmt") == "hhp":
        hint = hh._own_code_page(raw, version)
    else:
        hint = kwargs.get("hhp_code_page")
    _text, enc = hh._load(raw, capped=True, hint_cp=hint, version=version)
    return enc.codec


def _gate_its(data: bytes) -> str:
    """xml_its.plan_document's upload checks: #509 decode, element budget, DTD internal
    subset (XXE), #792 tag budget, well-formedness, declared entities."""
    from app.filters import xml_its

    text, _bom, enc = xml_its._decode(bytes(data))
    xml_its.check_xml_budget(text)
    xml_its._parse_tree(text)
    xml_its._lex(text)  # element depth (MAX_DEPTH) and balance, as plan_document refuses
    return enc


def _gate_xml(data: bytes) -> str:
    """LocOS's .xml router (xml_resource.pick): Android strings and Java XML properties get
    the resource filters' load (decode, budgets, internal subset, well-formedness, undefined
    entities); every other root the ITS checks."""
    from app.filters import xml_resource
    from app.filters.xml_resource_common import XmlResourceFilter

    picked = xml_resource.pick(bytes(data))
    if isinstance(picked, XmlResourceFilter):
        return picked._load(bytes(data))[2]
    return _gate_its(data)


def _gate_xslt(data: bytes) -> str:
    from app.filters import xslt_filter

    _text, codec, _bom, _doctype, _count = xslt_filter._load(bytes(data), capped=True)
    return codec


def _gate_xmb(data: bytes) -> str:
    from app.filters import angular_xmb_filter as ax
    from app.filters.base import encode_text_lossless, read_text_upload

    raw = bytes(data)
    ax.check_xmb_budget(raw)
    text, enc = read_text_upload(raw, sig=True, declared=True)
    ax._parse_tree(encode_text_lossless(text))
    return enc


def _gate_ttml(data: bytes) -> str:
    from app.filters import ttml_filter as tt
    from app.filters.base import encode_text_lossless, read_text_upload

    raw = bytes(data)
    text, enc = read_text_upload(raw, sig=True, declared=True)
    tt.check_ttml_budget(raw)
    if enc not in ("utf-8", "utf-16") and tt._declared_codec(raw) in (None, "utf-8", "utf-16"):
        raise UnsupportedEncodingError(
            "This TTML file is not UTF-8 and does not declare its encoding "
            '(<?xml version="1.0" encoding="..."?>). Re-save it as UTF-8, or add the '
            "declaration."
        )
    tt._parse_tree(encode_text_lossless(text))
    return enc


# Wave 6 (markup, subtitles, legacy, the remaining resource formats). Each gate is the Python
# filter's own decode step (the #509 refusals, the line and size budgets, the format's encoding
# rules: rc code page pragmas, XPress Tags `<e>` tags, UTF-8-only desktop and catkeys, DXF's
# $DWGCODEPAGE, the MO header charset) and the cheap structural refusals it makes before any
# unit walk (not an SCC / EBU STL / Qt Linguist file, binary DXF, ambiguous .sub, PGP .asc, NUL
# in an .stf). The subtitle readers use the BOM read version 2 (#799.4 / #800.4: a UTF-8 BOM
# before legacy bytes is a mark), the version every new upload gets. Each returns the label the
# Python filter records as `source_encoding` ("utf-8" where it records none).


def _gate_module_label(module: str, fn: str = "_decode"):
    """The Python filter's `<module>.<fn>(data)` -> (text, label, ...)."""

    def gate(data: bytes) -> str:
        import importlib

        return importlib.import_module(f"app.filters.{module}").__dict__[fn](bytes(data))[1]

    return gate


def _gate_utf8_only(module: str):
    """desktop / catkeys: size cap, strict UTF-8, NUL, line budget (no label recorded)."""

    def gate(data: bytes) -> str:
        import importlib

        importlib.import_module(f"app.filters.{module}")._decode(bytes(data))
        return "utf-8"

    return gate


def _gate_bom(data: bytes) -> str:
    from app.filters.base import read_text_bom

    return read_text_bom(bytes(data))[1]  # #509 decode + #571 line budget, BOM read v2


def _gate_sub(data: bytes) -> str:
    from app.filters.subtitle_text_filter import sniff_sub

    sniff_sub(bytes(data))  # VobSub, mixed MicroDVD/SubViewer, unrecognised (before decoding)
    return _gate_bom(data)


def _gate_scc(data: bytes) -> str:
    """scc_filter._read's pre-walk refusals: the #509 decode, the Scenarist header line and the
    per-line code-word budget (its line grammar refusals Bilingua makes too: the Python filter
    words them, see _REFUSAL_BY_PYTHON). The walk itself is not run twice (BILI-18)."""
    from app.filters import scc_filter as sc
    from app.filters.base import read_text_upload

    text, _enc = read_text_upload(bytes(data), sig=True)
    lines = sc._split_lines(text)
    first = next((i for i, (c, _e) in enumerate(lines) if c.strip()), None)
    if first is None or lines[first][0].strip() != sc.HEADER:
        raise sc.SccParseError(f"Not a Scenarist SCC file: the first line must be '{sc.HEADER}'.")
    for i, (content, _eol) in enumerate(lines):
        # more than MAX words of 4 characters and a separator cannot be shorter than this
        if i > first and len(content) >= 5 * sc.MAX_WORDS_PER_LINE:
            m = sc._TC_RE.match(content.strip())
            if m and len(m.group(3).split()) > sc.MAX_WORDS_PER_LINE:
                raise InputLimitError(
                    f"SCC line {i + 1} has more than {sc.MAX_WORDS_PER_LINE} code words; refused."
                )
    return "utf-8"


def _gate_stl(data: bytes) -> str:
    from app.filters import stl_filter

    stl_filter._read(bytes(data))  # GSI block, TTI blocks whole and budgeted, code table
    return "utf-8"


def _gate_stf(data: bytes) -> str:
    """stf_filter._load without building the table: the #509 decode and the NUL refusal."""
    from app.filters.base import read_text_upload

    text, enc = read_text_upload(bytes(data))
    if "\x00" in text:
        raise UnsupportedEncodingError(
            "This .stf file contains NUL bytes, so it is not a Translation Workbench text "
            "export. Re-export it from Translation Workbench or re-save it as UTF-8."
        )
    return enc


def _gate_dxf(data: bytes) -> str:
    from app.filters import dxf_filter

    raw = bytes(data)
    _units, acadver, codepage = dxf_filter._walk(raw)  # binary DXF, budgets, pair grammar
    return dxf_filter._codec(raw, acadver, codepage)  # $DWGCODEPAGE / AC1021 UTF-8 rules


def _gate_rc(data: bytes) -> str:
    from app.filters import rc_filter

    text, enc = rc_filter._decode(bytes(data))  # BOMs, code page pragmas, ANSI page
    rc_filter._budget(text)
    return enc


def _gate_plist(fmt: str):
    """apple_filter.read_plist's pre-scan refusals: the markup budget, the binary plist version
    and object budget, the XML decode, an OpenStep (non-XML) file, a declared non-UTF encoding,
    and an internal DTD or entity declaration (XXE, as _scan_xml refuses it first). The strict
    scan and the plistlib cross-check are not run twice (BILI-18): a file Bilingua refuses goes
    to the Python filter for its sentence (_REFUSAL_BY_PYTHON). Returns the Python codec."""

    def gate(data: bytes) -> str:
        from app.filters import apple_filter as af

        raw = bytes(data)
        af.check_plist_budget(raw)
        if raw.startswith(b"bplist"):
            if not raw.startswith(b"bplist00"):
                raise af.PlistError("unsupported binary property list version (only bplist00)")
            if len(raw) >= 32 and int.from_bytes(raw[-24:-16], "big") > (
                40 * config.MAX_DOCUMENT_SEGMENTS
            ):
                raise InputLimitError(
                    "This property list has far more entries than MAX_DOCUMENT_SEGMENTS "
                    f"allows ({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
                )
            return "utf-8"
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            bom, codec = raw[:2], ("utf-16-le" if raw[:2] == b"\xff\xfe" else "utf-16-be")
        elif raw.startswith(b"\xef\xbb\xbf"):
            bom, codec = raw[:3], "utf-8"
        else:
            bom, codec = b"", "utf-8"
        try:
            text = raw[len(bom) :].decode(codec)
        except UnicodeDecodeError as exc:
            raise af.PlistError(
                f"property list is not valid {codec.upper()} text; re-save it as UTF-8 XML"
            ) from exc
        head = text.lstrip()[:200]
        if not head.startswith("<"):
            raise af.PlistError(
                "not an XML or binary property list (old-style OpenStep plists are not "
                "supported; convert with `plutil -convert xml1`)"
            )
        decl = re.match(r"<\?xml[^>]*encoding\s*=\s*[\"']([^\"']+)", head)
        if decl and decl.group(1).strip().lower().replace("_", "-") not in (
            "utf-8",
            "utf8",
            "utf-16",
            "utf-16le",
            "utf-16be",
        ):
            raise af.PlistError("property list declares an encoding other than UTF-8/UTF-16")
        if "<!ENTITY" in text or re.search(r"<!DOCTYPE[^>]*\[", text):
            raise af.PlistError("property list declares an internal DTD or entities; refused")
        return codec

    return gate


def _gate_mo(data: bytes) -> str:
    from app.filters import mo_filter

    mo_filter.read_entries(bytes(data))  # magic, revision, offsets, header charset, sysdep
    return "utf-8"


def _gate_csv(data: bytes) -> str:
    from app.filters import csv_filter

    _text, how = csv_filter._decode(bytes(data))
    return how.get("converted") or "utf-8"  # as CsvFilter.parse records it


def _gate_xtg(data: bytes) -> str:
    from app.filters import quark_tags_filter

    return quark_tags_filter._decode(bytes(data))[1].codec


def _gate_qt(data: bytes) -> str:
    from app.filters.xml_resource_common import Splicer

    text, _bom, enc, root = _python_filter(_QT_TS)._load(bytes(data))  # decode, budgets, XXE
    Splicer(text, root)  # the Python build must be able to map every element
    return enc


# Wave 5 export guards. The Python HTML-family writers re-parse every build as a browser does
# (#676, html_guard.check_build) and refuse an export whose page gains an element, an
# attribute or a script/style body; the server-page writers escape translated text so it can
# never open a code island; the e-mail writer keeps every MIME part and header. The adapter
# runs the same checks on what Bilingua wrote, against the same file built WITHOUT
# translations (the original, or for a localized build the original with only the language
# declaration, so a declared lang/dir is never taken for injected markup). XML formats: the
# element tree (names and attribute names, comments and PIs) must be the original's, and the
# output must still be well-formed. A failed check is UnsafeMarkupRefused (export refuses, as
# for the Python filters); nothing is ever exported unchecked.


def _decode_page(raw: bytes) -> str:
    from app.filters.base import decode_text_lossless

    return decode_text_lossless(bytes(raw), sig=True, declared=True, rare=True)


# The charset a page declares (meta charset, http-equiv content, the XML declaration) follows
# the encoding the writer chose, which is not markup a translation adds (the Python writers fix
# it after their #676 check, in encode_like); both sides of a guard compare it as one constant.
_CHARSET_LABEL = re.compile(
    r"""(?i)(charset\s*=\s*["']?|<\?xml[^>]{0,200}?encoding\s*=\s*["'])[\w.:-]+"""
)


def _neutral_charset(text: str) -> str:
    return _CHARSET_LABEL.sub(lambda m: m.group(1) + "-", text)


def _unsafe(msg: str) -> Exception:
    from app.filters.html_guard import UnsafeMarkupRefused

    return UnsafeMarkupRefused(msg)


def _server_guard(sp, before: str, after: str) -> None:
    from collections import Counter

    from app.filters.html_guard import check_build
    from app.filters.server_page_filter import _S_CLOSE, _S_OPEN, _SENT_RE

    mb, codes_b, _inl = sp._mask(_neutral_charset(before))
    ma, codes_a, _inl = sp._mask(_neutral_charset(after))
    # a code block is one opaque run whatever its place in the page: the sentinel numbers
    # (positions) are not compared, the code itself is (below)
    one = _S_OPEN + "0" + _S_CLOSE
    mb, ma = _SENT_RE.sub(one, mb), _SENT_RE.sub(one, ma)
    # no code block that the source does not have (a dropped code token is Bilingua's to
    # restore, as LocOS's writer re-appends it: not a safety question)
    if Counter(codes_a) - Counter(codes_b):
        raise _unsafe(
            f"This {sp.label} cannot be exported safely: the translated file holds server code "
            "the source page does not have (a translation must never add or change code). "
            "Check the segments that contain code characters and export again."
        )
    check_build(mb, ma)


def _guard_html(before: bytes, after: bytes, locale: str | None = None) -> None:
    from app.filters.html_guard import check_build

    b, a = _neutral_charset(_decode_page(before)), _neutral_charset(_decode_page(after))
    server = _html_server(b)
    if server is not None:
        _server_guard(server, b, a)
    else:
        check_build(b, a)


def _guard_server(fmt: str):
    def guard(before: bytes, after: bytes, locale: str | None = None) -> None:
        sp = _python_filter(fmt)
        if fmt == "php":
            sp = sp._pick(bytes(before))
            if not hasattr(sp, "_mask"):  # a language array: its writer escapes PHP strings
                return
        _server_guard(sp, _decode_page(before), _decode_page(after))

    return guard


def _guard_sitemap(before: bytes, after: bytes, locale: str | None = None) -> None:
    from app.filters import htmlhelp_filter as hh
    from app.filters.html_guard import check_build

    b, _e = hh._decode(bytes(before))
    a, _e = hh._decode(bytes(after))
    check_build(_neutral_charset(b), _neutral_charset(a), extra=hh._FREE_IN_SITEMAP, xml=False)


_PAGE_MEMBER = (".xhtml", ".html", ".htm", ".xml", ".opf", ".ncx", ".fltoc", ".flvar", ".flsnp")
_PAGE_MEMBER += (".flglo", ".fltar", ".flprj", ".flpgl", ".flmsk")


def _guard_package(extra_of):
    def guard(before: bytes, after: bytes, locale: str | None = None) -> None:
        import io

        from app.filters.base import decode_text_lossless
        from app.filters.html_guard import check_build

        with (
            zipfile.ZipFile(io.BytesIO(bytes(before))) as zb,
            zipfile.ZipFile(io.BytesIO(bytes(after))) as za,
        ):
            nb = [i.filename for i in zb.infolist()]
            na = [i.filename for i in za.infolist()]
            if sorted(nb) != sorted(na):
                raise _unsafe(
                    "This package cannot be exported safely: the translated package does not "
                    "hold the same files as the source."
                )
            for name in nb:
                rb, ra = zb.read(name), za.read(name)
                if rb == ra or not name.lower().endswith(_PAGE_MEMBER):
                    continue
                b = decode_text_lossless(rb, sig=True, declared=True)
                a = decode_text_lossless(ra, sig=True, declared=True)
                check_build(_neutral_charset(b), _neutral_charset(a), extra=extra_of())

    return guard


def _flare_extra():
    from app.filters.flare_filter import _FREE_IN_FLARE

    return _FREE_IN_FLARE


def _guard_eml(before: bytes, after: bytes, locale: str | None = None) -> None:
    """The e-mail writer's invariants: the same MIME tree (no translation may end its part
    or start a new one), the same header field names, every HTML part safe (#676)."""
    from collections import Counter

    from app.filters import email_filter as em
    from app.filters.html_guard import check_build

    # header fields the writer itself adds or relabels when a translation needs another
    # charset or transfer encoding (email_filter._encode_part)
    writer = {"content-transfer-encoding", "mime-version", "content-language"}

    def names(top):
        return Counter(k.lower() for k in top.headers.keys())  # noqa: SIM118 (Message)

    tb, sb, _eol = em._walk(bytes(before))
    try:
        ta, sa, _eol = em._walk(bytes(after))
    except ValueError as exc:
        raise _unsafe(
            "This email cannot be exported safely: the translated message cannot be read "
            f"again ({exc})"
        ) from exc
    added = {k for k in names(ta) - names(tb) if k not in writer}
    leaves_b = [p.ctype for p in em._leaves(tb)]
    leaves_a = [p.ctype for p in em._leaves(ta)]
    if added or leaves_a != leaves_b or [s.kind for s in sb] != [s.kind for s in sa]:
        raise _unsafe(
            "This email cannot be exported safely: in the translated message the MIME parts or "
            "header fields are not the source message's (a translation must never add a header "
            "or a part)."
        )
    for x, y in zip(sb, sa, strict=True):
        if x.kind == "html":
            check_build(x.text, y.text)


# Attributes a localized build declares the target language and direction with (xml:lang,
# lang, dir, its:dir, TTML/DFXP tts:direction and writingMode, whatever their namespace): not
# compared, like html_guard's language declaration set.
_XML_DECLARATION_ATTRS = {"lang", "dir", "direction", "writingMode"}


def _xml_shape(raw: bytes):
    """(the set of element signatures (name, attribute names but the language declaration),
    the set of comment and PI texts) of a well-formed XML document parsed with entities off,
    or None when it does not parse."""
    from lxml import etree

    from app.filters.xml_guard import make_hardened_parser

    try:
        root = etree.fromstring(bytes(raw), parser=make_hardened_parser())
    except (etree.XMLSyntaxError, ValueError):
        return None
    # the (tag, attribute names) pairs first, each distinct pair made a signature once: a file
    # has thousands of elements and a few dozen distinct pairs (FINAL3 gate: the per-element
    # sort was most of an XMB build's export check)
    pairs, other = set(), set()
    for node in root.iter():
        tag = node.tag
        if isinstance(tag, str):
            pairs.add((tag, tuple(node.keys())))
        else:
            other.add(etree.tostring(node, with_tail=False))
    skip = _XML_DECLARATION_ATTRS
    els = {
        (tag, tuple(sorted(a for a in names if a.rsplit("}", 1)[-1] not in skip)))
        for tag, names in pairs
    }
    return els, other


def _language_elements(locale: str | None) -> set:
    """Element names a multilingual XML layout adds for the target language (the locale tag
    or its language subtag, as xml_multilingual names a new language sibling)."""
    if not locale:
        return set()
    tag = locale.replace("_", "-")
    return {tag, tag.split("-")[0], tag.lower(), tag.replace("-", "_")}


# Angular: an XMB source is exported as an XTB translation bundle (angular_xmb_filter), whose
# elements the source does not have.
_XTB_VOCABULARY = {
    ("translationbundle", ("lang",)),
    ("translationbundle", ()),
    ("translation", ("id",)),
    ("ph", ("name",)),
}


def _guard_xml_with(allowed: set):
    def guard(before: bytes, after: bytes, locale: str | None = None) -> None:
        """No kind of element (name and attribute names) and no comment or PI that the source
        lacks: a translator may drop or move an inline tag, never add markup; the output must
        stay well-formed. A localized build may add the target language's element (a
        multilingual layout's new sibling, a copy of the source language's)."""
        shape_b = _xml_shape(before)
        if shape_b is None:
            return  # the stored original does not parse here: nothing to compare against
        shape_a = _xml_shape(after)
        if shape_a is None:
            raise _unsafe(
                "This XML file cannot be exported safely: the translated file is not "
                "well-formed XML."
            )
        langs = _language_elements(locale)
        new = {
            sig
            for sig in shape_a[0] - shape_b[0] - allowed
            if not (isinstance(sig[0], str) and sig[0].rsplit("}", 1)[-1] in langs)
        }
        if new or shape_a[1] - shape_b[1]:
            raise _unsafe(
                "This XML file cannot be exported safely: the translated file has elements, "
                "attributes, comments or processing instructions the source does not have (a "
                "translation must never add markup)."
            )

    return guard


_guard_xml = _guard_xml_with(set())


_BUILD_GUARDS = {
    "html": _guard_html,
    "aspx": _guard_server("aspx"),
    "jsp": _guard_server("jsp"),
    "erb": _guard_server("erb"),
    "php": _guard_server("php"),
    "phtml": _guard_server("phtml"),
    "shtml": _guard_server("shtml"),
    "hhc": _guard_sitemap,
    "hhk": _guard_sitemap,
    "epub": _guard_package(frozenset),
    "flprjzip": _guard_package(_flare_extra),
    "eml": _guard_eml,
    "xml": _guard_xml,
    "dita": _guard_xml,
    "docbook": _guard_xml,
    "xslt": _guard_xml,
    "xmb": _guard_xml_with(_XTB_VOCABULARY),
    "xtb": _guard_xml,
    "ttml": _guard_xml,
}


# Wave 4: on a new import, a refusal from Bilingua is handed to the Python filter, which
# refuses the same file with its own class and sentence (the Office filters have many: not a
# package, password-protected, no workbook, a Strict part it cannot map, a damaged part); only
# when the Python filter reads the file does Bilingua's own refusal stand (an on-purpose
# difference, such as a `../` member name). The Python parse runs on this rare path only.
# Wave 4 formats whose Python filter passes every target through xml_target_text (#243) before
# writing it: XML 1.0 characters only (a lone surrogate cannot even reach the engine), Word's
# pasted vertical tab and form feed as new lines. MIF is not XML.
_XML_TARGET = {"docx", "xlsx", "pptx", "odt", "ods", "odp", "odg", "vsdx", "idml", "icml"}
# wave 5: the XML writers that pass targets through xml_target_text (angular_xmb, epub, flare,
# ttml, xml_its / xml_multilingual, xml_resource_common, xslt)
_XML_TARGET |= {"epub", "flprjzip", "xml", "dita", "docbook", "xslt", "xmb", "xtb", "ttml"}
# wave 6: Qt Linguist (xml_resource_common); an XML plist (apple_filter.escape_xml, not binary)
_XML_TARGET |= {"ts"}
_XML_PLIST = {"plist", "stringsdict"}
_REFUSAL_BY_PYTHON = {
    "docx",
    "xlsx",
    "pptx",
    "odt",
    "ods",
    "odp",
    "odg",
    "vsdx",
    "idml",
    "icml",
    "mif",
    # wave 5: every format (server pages, e-mail, packages and XML each have many refusals
    # with their own sentences, and Bilingua's own copies come as code `parse`)
    "html",
    "epub",
    "eml",
    "flprjzip",
    "aspx",
    "jsp",
    "erb",
    "php",
    "phtml",
    "shtml",
    "mdx",
    "hhc",
    "hhk",
    "hhp",
    "xml",
    "dita",
    "docbook",
    "xslt",
    "xmb",
    "xtb",
    "ttml",
}


# Decode gates (#509, decision 22): per wave-2 format, the Python filter's OWN decode step,
# run before Bilingua on a new import. It refuses what LocOS refuses with LocOS's class and
# sentence (UnsupportedEncodingError "...; re-save the file as UTF-8.", the line budget),
# returns the encoding label the Python filter records as `source_encoding`, and under
# ingest's code page hint sets `hint.used` and the encoding warnings exactly as the Python
# filter does (read_text_upload). Only the bytes are decoded: units, codes and the
# skeleton still come from Bilingua. Formats without a gate keep Bilingua's own reading
# (md, json, po: wave 1).
# Formats whose Python `build` keeps the bytes of a stored original that is neither UTF-8 nor
# Windows-1252 (a pre-#509 import) although its `parse` refuses them (txt: build_text, B-bytes).
_BUILD_BY_PYTHON = frozenset({"txt"})

# The formats whose Python filter takes `lenient` (html_filter, server_page_filter): the lenient
# read decodes UTF-8 with `replace` instead of the #509 gate.
_LENIENT_FORMATS = frozenset({"html", "aspx", "jsp", "erb", "php", "phtml", "shtml"})

_DECODE_GATES = {
    "txt": _gate_text(sig=True),
    "srt": _gate_text(sig=True),
    "json5": _gate_text(sig=True),
    "yaml": _gate_text(sig=True),
    "js": _gate_text(sig=False),
    "resx": _gate_text(sig=True, declared=True),
    "wxl": _gate_text(sig=True, declared=True),
    "strings": _gate_resource("strings"),
    "properties": _gate_resource("properties"),
    "ini": _gate_resource("ini"),
    "resjson": _gate_resource("resjson"),
    "ftl": _gate_module("fluent_filter", None),
    "dtd": _gate_module("dtd_filter", None),
    "toml": _gate_module("toml_filter", None),
    "xcstrings": _gate_module("apple_filter", None),
    "xaml": _gate_module("xaml_filter", 1),
    # wave 3: refusals only (see _gate_xliff)
    "xliff": _gate_xliff,
    "sdlxliff": _gate_cat,
    "mqxliff": _gate_cat,
    "mxliff": _gate_cat,
    "txlf": _gate_cat,
    "txml": _gate_txml,
    "ttx": _gate_ttx,
    "xlz": _gate_xliff_package("xlz"),
    "mqxlz": _gate_xliff_package("mqxlz"),
    "sdlrpx": _gate_sdl_package,
    # wave 4: pre-parse refusals only (see _gate_docx)
    "docx": _gate_docx,
    "xlsx": _gate_none,
    "pptx": _gate_pptx,
    "odt": _gate_odf,
    "ods": _gate_odf,
    "odp": _gate_odf,
    "odg": _gate_odf,
    "vsdx": _gate_vsdx,
    "idml": _gate_none,
    "icml": _gate_icml,
    "mif": _gate_mif,
    # wave 5: the Python filter's upload refusals and its source_encoding label
    "html": _gate_html,
    "epub": _gate_epub,
    "eml": _gate_eml,
    "flprjzip": _gate_flare,
    "aspx": _gate_server("aspx"),
    "jsp": _gate_server("jsp"),
    "erb": _gate_server("erb"),
    "php": _gate_php,
    "phtml": _gate_server("phtml"),
    "shtml": _gate_server("shtml"),
    "mdx": _gate_mdx,
    "hhc": _gate_htmlhelp,
    "hhk": _gate_htmlhelp,
    "hhp": _gate_htmlhelp,
    "xml": _gate_xml,
    "dita": _gate_its,
    "docbook": _gate_its,
    "xslt": _gate_xslt,
    "xmb": _gate_xmb,
    "xtb": _gate_xmb,
    "ttml": _gate_ttml,
    # wave 6: the Python filter's decode step and cheap structural refusals (see _gate_bom)
    "adoc": _gate_module_label("asciidoc_filter"),
    "rst": _gate_module_label("rst_filter"),
    "wiki": _gate_text(sig=True),
    "dokuwiki": _gate_text(sig=True),
    "tex": _gate_text(sig=True),
    "ass": _gate_bom,
    "sbv": _gate_bom,
    "sub": _gate_sub,
    "scc": _gate_scc,
    "stl": _gate_stl,
    "stf": _gate_stf,
    "dxf": _gate_dxf,
    "rc": _gate_rc,
    "desktop": _gate_utf8_only("desktop_filter"),
    "catkeys": _gate_utf8_only("catkeys_filter"),
    "plist": _gate_plist("plist"),
    "stringsdict": _gate_plist("stringsdict"),
    "mo": _gate_mo,
    "csv": _gate_csv,
    "tsv": _gate_csv,
    "xtg": _gate_xtg,
    "ts": _gate_qt,
}
_WAVE_6 = ("adoc", "rst", "wiki", "dokuwiki", "tex", "ass", "sbv", "sub", "scc", "stl", "stf")
_WAVE_6 += ("dxf", "rc", "desktop", "catkeys", "plist", "stringsdict", "mo", "csv", "tsv")
_WAVE_6 += ("xtg", "ts")
# wave 6: a Bilingua refusal of a new import goes to the Python filter (its class and sentence)
_REFUSAL_BY_PYTHON |= set(_WAVE_6)
# Wave 6: when Bilingua cannot write a translation into the file (code `unwritable`: a caption
# that would drop out, read back as a timecode or not fit, a character the code table cannot
# hold), the Python writer is asked; its refusal (the subtitle filters' CaptionTargetError with
# the cue number, which export shows as such) is raised instead (_python_write_refusal).
_WRITE_REFUSAL_BY_PYTHON = set(_WAVE_6)
# HTML Help (W5 B hh-2): Bilingua keeps a .hhc/.hhk/.hhp in its ANSI code page (the target's
# when localized) and refuses (`unwritable`) a translation that code page cannot hold, as
# LocOS does; the Python writer's HtmlHelpEncodingError (code page, first character, option)
# is raised instead, as for the wave-6 caption refusals.
_HTMLHELP = ("hhc", "hhk", "hhp")
_WRITE_REFUSAL_BY_PYTHON |= set(_HTMLHELP)
# HTMLHELP_UTF8_FALLBACK as the wheel's option: text the code page cannot hold is written as
# UTF-8 (meta charset rewritten) instead of refused, as htmlhelp_filter._finish does.
_HH_UTF8_FALLBACK = "htmlhelp.utf8_fallback"
# Gates that read the parse keywords (the HTML Help project code page, decision 21).
_GATE_KWARGS = {"hhc", "hhk", "hhp"}
# Wave 3: on a new import a `parse` refusal shows the parser's own sentence (`.detail`, the
# LocOS filter's wording in the profile) as the Python filter does, without the engine's
# "Bilingua could not read ... not a <format> file:" frame.
_DETAIL_MESSAGE = {
    "xliff",
    "sdlxliff",
    "mqxliff",
    "mxliff",
    "txlf",
    "txml",
    "ttx",
    "xlz",
    "mqxlz",
    "sdlrpx",
}


def decode_gate(fmt: str, data: bytes, kwargs: dict | None = None) -> str | None:
    """The Python filter's decode step for `fmt` (see _DECODE_GATES): the encoding label it
    records, or None for a format without a gate. Raises the Python filter's refusal.
    `kwargs`: the parse keywords, for the gates that read them (_GATE_KWARGS)."""
    gate = _DECODE_GATES.get(fmt)
    if gate is None:
        return None
    if fmt in _GATE_KWARGS:
        return gate(bytes(data), {**(kwargs or {}), "_fmt": fmt})
    return gate(bytes(data))


def _xcloc_meta(data: bytes) -> dict:
    """The bundle keys the Python xcloc filter records (bundle prefix, XLIFF members,
    contents.json regions), read from the zip listing and contents.json; {} when the archive
    does not have that shape (Bilingua's own refusals stand)."""
    import io
    import zipfile

    from app.filters import xcloc_filter as xf

    try:
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
            prefix, xliffs = xf._layout(zf)
            _raw, meta = xf._read_contents(zf, prefix)
    except (ValueError, OSError, zipfile.BadZipFile, KeyError):
        return {}
    return {
        "bundle": prefix,
        "xliff_files": xliffs,
        "development_region": str(meta.get("developmentRegion") or ""),
        "target_locale": str(meta.get("targetLocale") or ""),
    }


def _sdl_package_meta(data: bytes) -> dict | None:
    """skeleton["sdl_package"] as SdlPackageFilter.parse records it: member, locale,
    package_guid, package, from the member blob's marker (for a raw package with one target
    file, from the blob LocOS would make of it). None when neither applies (the gate refused)."""
    from app.filters import sdl_package as sp

    meta = sp.member_meta(data)
    if meta is None:
        try:
            info = sp.inspect(data)
        except (ValueError, OSError):
            return None
        if len(info.targets) != 1:
            return None
        meta = sp.member_meta(sp.member_blob(data, info, info.targets[0])) or {}
    return {
        "member": meta.get("member"),
        "locale": meta.get("locale"),
        "package_guid": meta.get("package_guid", ""),
        "package": meta.get("package", ""),
    }


_DOCX_BILINGUAL = "docx_bilingual_table"


def _docx_file_warnings(warnings: list[dict], data: bytes) -> list[dict]:
    """skeleton["file_warnings"] of a DOCX as DocxFilter.parse writes it: the external resource
    findings (docx_resources, a scan of the package bytes) and, for a Storyline "Export to
    Word" table, docx_bilingual's notice with its counts (rows, tables, empty rows), which the
    upload doors and the e-learning surfaces read. The table is LocOS's own detection
    (docx_bilingual.translation_paragraphs) on the main part, read only when it has a table;
    Bilingua already gives the Translation column as the only units."""
    import io

    from app.filters.docx_resources import external_resource_warnings

    out = [w for w in warnings if w.get("kind") != _DOCX_BILINGUAL]
    out.extend(external_resource_warnings(data))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        part = _docx_main_part(zf)
        raw = zf.read(part) if part in zf.namelist() else b""
    if b"tbl" in raw:  # w:tbl under any prefix
        from lxml import etree

        from app.filters import docx_bilingual

        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
        found = docx_bilingual.translation_paragraphs(etree.fromstring(raw, parser))
        if found is not None:
            out.append(docx_bilingual.notice(found[1]))
    return out


def _docx_main_part(zf) -> str:
    """The main document part named by the package relationships (word/document.xml)."""
    try:
        rels = zf.read("_rels/.rels").decode("utf-8", "replace")
    except KeyError:
        return "word/document.xml"
    for m in re.finditer(r"<Relationship\b[^>]*>", rels):
        el = m.group(0)
        if re.search(r'Type="[^"]*/relationships/officeDocument"', el):
            t = re.search(r'Target="([^"]+)"', el)
            if t:
                return t.group(1).lstrip("/")
    return "word/document.xml"


# A Bilingua warning the Python filter records as a skeleton key instead: TTX text outside
# every <Tu> (ttx_filter: skeleton["unsegmented_text"] = number of runs, no file warning).
_TTX_UNSEGMENTED = "ttx_unsegmented_text"
_RUNS_RE = re.compile(r"^(\d+) text run")
# Another: one per XLIFF member of an xlz/mqxlz package, part = member path, message = the
# member's slot count (protected segments included), in member order (bilingua_xliffpkg
# MEMBER_SLOTS, W3 B14). The Python package filter records skeleton["package"] from it.
_MEMBER_SLOTS = "locos_member_slots"
_XLIFF_PACKAGES = {"xlz", "mqxlz"}


def _package_listing(doc: dict, locos: list[dict], data: bytes) -> dict:
    """skeleton["package"] as xliff_package.parse_package records it: per XLIFF member its
    path, p_index offset (the slot counts of the members before it) and the number of units
    it gave, at most MAX_LISTED members; and the number of other (non-XLIFF) files."""
    import io

    from app.filters import xliff_package as xp

    where: dict = {}
    for unit in doc.get("units") or []:
        loc = (unit.get("context") or {}).get("location")
        where[unit.get("id")] = loc
        for seg in unit.get("segments") or []:
            where[seg.get("id")] = loc
    per: dict = {}
    for u in locos:
        loc = where.get(u["segment_id"])
        per[loc] = per.get(loc, 0) + 1
    listing: list[dict] = []
    offset = 0
    for w in doc.get("warnings") or []:
        if not isinstance(w, dict) or w.get("kind") != _MEMBER_SLOTS:
            continue
        path = w.get("part") or ""
        if len(listing) < xp.MAX_LISTED:
            listing.append({"path": path, "offset": offset, "units": per.get(path, 0)})
        with contextlib.suppress(ValueError):
            offset += int(w.get("message") or 0)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        other = sum(1 for i in zf.infolist() if not i.is_dir() and not xp._is_xliff_member(i))
    return {"xliff_files": listing, "other_files": other}


def _xcstrings_source_language(data: bytes) -> str | None:
    """The catalog's `sourceLanguage` (the Python filter records it as `source_language`)."""
    try:
        obj = json.loads(bytes(data).decode("utf-8-sig"))
    except (ValueError, RecursionError):
        return None
    lang = obj.get("sourceLanguage") if isinstance(obj, dict) else None
    return lang if isinstance(lang, str) else None


def _unit_extra(doc, name: str):
    """The first value of a unit extra `name` in the Document (a DocumentHandle), or None."""
    for entry in _extras(doc, [name]):
        value = entry["extra"].get(name)
        if value is not None:
            return value
    return None


def _empty_refusal(fmt: str, options: dict) -> str | None:
    """The Python filter's own sentence for a file with nothing to translate, for the formats
    that refuse it in the filter (most return zero units and ingest refuses, #573)."""
    if fmt == "xaml":
        hint = (
            ""
            if options.get("xaml.view_attributes")
            else " View attributes (Content, Text, ToolTip, Header, Title) are only "
            "translated when XAML_VIEW_ATTRIBUTES is on."
        )
        return "This XAML file has no sys:String or x:String resources to translate." + hint
    return None


# Formats whose Python filter refuses some files with nothing to translate, with a sentence
# that says why (TTX: not segmented, or every segment already translated). On zero units the
# adapter lets that filter decide (see parse).
_EMPTY_BY_PYTHON = {"ttx", "desktop", "catkeys"}  # wave 6: "no Name ... to translate"


_JS_NOT_STATIC = "is not a plain JavaScript/TypeScript locale module"


def _import_refusal(fmt: str, exc: BilinguaParseError) -> Exception:
    """A Bilingua refusal of a NEW import in the class the Python filter raises for it:
    `encoding` is the #509 UnsupportedEncodingError (422 with the re-save sentence on every
    door), a js module that is not a static message export the Python filter's
    InputLimitError with its sentence. Anything else stays as it is."""
    if exc.code == "encoding":
        return BilinguaEncodingError(str(exc), "encoding", exc.detail)
    # js keeps code `parse` for a module that is not static (LocOS classes it InputLimitError):
    # the parser's own sentence (`.detail`, no engine prefix) is the Python filter's sentence
    if fmt == "js" and exc.code == "parse" and _JS_NOT_STATIC in exc.detail:
        at = exc.detail.find("This file " + _JS_NOT_STATIC)
        return BilinguaModuleRefused(exc.detail[at:] if at >= 0 else exc.detail)
    if fmt in _DETAIL_MESSAGE and exc.code == "parse" and exc.detail and exc.detail != str(exc):
        return BilinguaParseError(exc.detail, exc.code, exc.detail)
    return exc


def check_input(fmt: str, data: bytes) -> None:
    """LocOS's input budgets and refusals before Bilingua sees the bytes: the upload size,
    the zip guard for package formats, and strict UTF-8 for the JSON family. Module level so
    a test harness can run exactly these checks."""
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise InputLimitError(
            f"This file is {len(data) // (1024 * 1024)} MB; the limit is "
            f"{config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB (MAX_UPLOAD_BYTES)."
        )
    if fmt in _ZIP_LABELS:
        from app.filters import zip_guard

        zip_guard.check(data, _ZIP_LABELS[fmt])
    if fmt == "xlsx":
        # the workbook's relationship, cache and cell budgets (#224 and the request-limit fixes)
        from app.filters import xlsx_guard

        # not a zip at all: Bilingua refuses it cleanly (xlsx_guard, as the Python filter,
        # would let zipfile's BadZipFile escape)
        with contextlib.suppress(zipfile.BadZipFile):
            xlsx_guard.check(data)
    if fmt in _STRICT_UTF8:
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise BilinguaParseError(
                "JSON file is not valid UTF-8 (byte 0x%02x at position %d could not be "  # noqa: UP031
                "decoded); re-save the file as UTF-8." % (data[exc.start], exc.start),
                "encoding",
            ) from exc


# --- the filter --------------------------------------------------------------------------


class BilinguaFilter(Filter):
    """A LocOS Filter over Bilingua for one canonical format. Not registered in the filter
    registry: `get_filter` hands it out through `select` when the switch says so."""

    fmt = "bilingua"
    exts: tuple = ()

    def __init__(self, fmt: str, skeleton: dict | None = None, *, modules: bool = True):
        self.stored = skeleton.get(SKELETON_KEY) if skeleton else None
        if fmt == "js" and (self.stored or {}).get("format") == _QT_TS:
            fmt = _QT_TS  # a Qt Linguist document (LocOS stores .ts uploads as js)
        self.fmt = fmt
        # js only: False when the switch lists Qt Linguist (`ts`) but not js, so a TypeScript
        # module stays with the Python js filter
        self._modules = modules
        # HTML: the unit list the document stores (export's HTML drift check, which the
        # Python html filter gets from export, runs here for a Bilingua document)
        self._stored_units = (
            skeleton.get("units") if fmt == "html" and isinstance(skeleton, dict) else None
        )
        # A Qt Linguist file is exported through LocOS's js filter (the `.ts` extension's
        # filter), whose B-bytes flags apply to it (an identity build is the upload)
        py = _python_filter("js" if fmt == _QT_TS else fmt)
        # Same B-bytes behaviour as the Python filter this stands in for.
        self.passthrough_untouched = getattr(py, "passthrough_untouched", True)
        self.writes_targets = bool(getattr(py, "writes_targets", False)) or fmt in (
            "po",
            "xliff",
            "sdlxliff",
        )
        # escapes_raw_text (export), and the parse keywords ingest offers a filter that takes
        # them: `comments=` (#690) and XLSX column mode (`columns=`), mapped to options
        for attr in ("escapes_raw_text", "takes_comments", "takes_columns"):
            if py is not None and hasattr(py, attr):
                setattr(self, attr, getattr(py, attr))
        # The tokens the last build kept as text or repaired, per segment id (a test and the
        # log read it), and the tag_integrity QA flag of each repaired segment, per p_index
        self.last_problems: dict = {}
        self.last_qa_flags: dict = {}
        # The last parse's extraction, for the build that follows it on this filter (BILI-18)
        self._memo: tuple | None = None

    # -- helpers --
    def _handoff(self, data: bytes):
        """The filter a `.ts` upload that is XML goes to, as the Python js filter hands it
        over (a Qt Linguist file, not a TypeScript module): the adapter for `ts` when the
        switch lists it (wave 6), else the registered Python `ts` filter, which makes a Python
        document, rebuilt by Python. A module goes to the Python js filter when the switch
        lists `ts` only. Only for a document without a Bilingua stamp (a stamped js document
        was a module, a stamped ts one is bound to `ts`). None for everything else."""
        if self.fmt != "js" or self.stored is not None:
            return None
        from app.filters import js_module_filter as jm

        py = _python_filter("js")
        qt = jm.JsModuleFilter._xml(py, data)
        if qt is not None:
            return BilinguaFilter(_QT_TS) if _QT_TS in enabled_formats() else qt
        return None if self._modules else py

    def records(self, data: bytes):
        """The key view (keys import) of a file this adapter hands to a Python filter: a
        TypeScript module while only Qt Linguist is switched on. Bilingua has no records();
        for every other file this is the AttributeError a filter without one gives."""
        other = self._handoff(bytes(data))
        if other is not None and not isinstance(other, BilinguaFilter):
            fn = getattr(other, "records", None)
            if callable(fn):
                return fn(data)
        raise AttributeError("'BilinguaFilter' object has no attribute 'records'")

    def _name(self, bl, data: bytes) -> str:
        _ids, name = _FORMATS[self.fmt]
        if self.fmt == "srt" and bl.detect(name, data) is None and bl.detect(_VTT_NAME, data):
            return _VTT_NAME
        if self.fmt == "html":
            # #672: a .html page that holds closed server code is read by the server-page
            # parser of its dialect (as LocOS does), so every code island stays a code
            server = self._html_dialect(data)
            if server is not None:
                return _HTML_SERVER_NAMES[server]
        if self.fmt == "php" and self._php_template(data):
            # LocOS's .php router (server_page_filter.PhpFilter): a page template, not a
            # language array, is read by the PHP template parser (Bilingua's phtml)
            return _HTML_SERVER_NAMES["phtml"]
        return name

    def _php_template(self, data: bytes) -> bool:
        """True when LocOS's .php router reads this file as a page template."""
        try:
            return hasattr(_python_filter("php")._pick(bytes(data)), "_mask")
        except ValueError:
            return False  # code only: the gate refuses it on import

    def _html_dialect(self, data: bytes) -> str | None:
        """The server-page format LocOS reads this .html as (#672), else None."""
        try:
            server = _html_server(_decode_page(data))
        except ValueError:
            return None  # a page mixing PHP and <% %>: the gate refuses it on import
        return getattr(server, "fmt", None) if server is not None else None

    def _check_input(self, data: bytes) -> None:
        check_input(self.fmt, data)

    def _profile_version(self) -> int | None:
        prof = (self.stored or {}).get("profile")
        if isinstance(prof, dict) and isinstance(prof.get("version"), int):
            return prof["version"]
        return None

    def _extract(
        self,
        bl,
        data: bytes,
        *,
        source_lang: str | None,
        target_lang: str | None = None,
        enforce_caps: bool = True,
        options: dict | None = None,
        checked: bool = False,
    ):
        """The Document of `data` as a DocumentHandle (SPEED2): immutable, kept by the wheel."""
        _trace("extract", self.fmt)
        if enforce_caps and not checked:
            self._check_input(data)
        kw: dict = {
            "source_lang": source_lang or None,
            # LocOS says es_ES, files say es-ES: Bilingua formats the tag per format.
            "target_lang": (target_lang or "").replace("_", "-") or None,
            "granularity": "paragraph",
            "profile": PROFILE,
        }
        pv = self._profile_version()
        if pv is not None:
            kw["profile_version"] = pv
        if enforce_caps:
            kw["limits"] = limits_from_config()
        if options:
            kw["format_options"] = dict(options)
        return _call(bl, self.fmt, bl.extract_handle, self._name(bl, data), data, **kw)

    def _extract_units(
        self,
        bl,
        data: bytes,
        *,
        memo: str,
        source_lang: str | None,
        target_lang: str | None = None,
        enforce_caps: bool = True,
        options: dict | None = None,
    ) -> tuple[object, list[dict], tuple]:
        """extract + locos_units, and the memo key of the call. The byte guard parses the
        original right before every build() (base._is_identity), and the build extracted the
        same bytes again with the same options. Now a parse that ends without a refusal keeps
        its (doc, units) on the filter (memo="keep": parse stores them last) and the next
        build takes them (memo="take", once) when bytes and options are the same: one extract
        and one locos_units less per export (BILI-18). The memo holds the DocumentHandle, which
        is immutable (locos_set_targets returns a new one), so neither call changes it."""
        key = (
            bytes(data),
            source_lang or None,
            target_lang or None,
            bool(enforce_caps),
            json.dumps(options or {}, sort_keys=True, default=str),
        )
        if memo == "take":
            held, self._memo = self._memo, None
            if held is not None and held[0] == key:
                return held[1], held[2], key
        doc = self._extract(
            bl,
            data,
            source_lang=source_lang,
            target_lang=target_lang,
            enforce_caps=enforce_caps,
            options=options,
            checked=memo == "keep",  # parse ran check_input itself
        )
        locos = _call(bl, self.fmt, doc.locos_units)
        return doc, locos, key

    def _options(self, kwargs: dict) -> dict:
        if self.stored is not None:  # a rebuild replays the options the import used
            stored = self.stored.get("options") or {}
            opts = {**stored, **options_from_kwargs(self.fmt, kwargs)}
        else:
            opts = {**install_options(self.fmt), **options_from_kwargs(self.fmt, kwargs)}
        if self.fmt in _HTMLHELP:
            # HTMLHELP_UTF8_FALLBACK is read when the file is WRITTEN (htmlhelp_filter._finish),
            # so it is the install's setting now, never a stamped value; never stamped either
            opts.pop(_HH_UTF8_FALLBACK, None)
            from app.filters.htmlhelp_filter import _utf8_fallback_enabled

            if _utf8_fallback_enabled():
                opts[_HH_UTF8_FALLBACK] = True
        return opts

    # -- Filter API --
    def parse(
        self,
        data: bytes,
        provider: str | None = None,
        *,
        lenient: bool = False,
        enforce_caps: bool = True,
        **kwargs,
    ) -> ParseResult:
        bl = _module()
        if bl is None:
            raise BilinguaUnavailable("The bilingua module is not installed.")
        data = bytes(data)
        other = self._handoff(data)
        if other is not None:
            if isinstance(other, BilinguaFilter):
                return other.parse(data, provider, lenient=lenient, enforce_caps=enforce_caps)
            return other.parse(data, provider)
        hint = _code_page_hint()
        source_lang = (getattr(hint, "locale", "") or None) if hint is not None else None
        if source_lang is None and self.stored:
            source_lang = self.stored.get("source_lang")
        if source_lang is None and kwargs.get("source_locale"):
            # the tabular filters' `source_locale` (CSV/TSV header planning): the language
            # a header names as the source column, the same project language as the hint
            source_lang = str(kwargs["source_locale"])
        options = self._options(kwargs)
        if lenient and self.fmt in _LENIENT_FORMATS:
            # export re-reading the stored original of a pre-#509 import: the Python filter
            # decodes UTF-8 with `replace` and never refuses (html_filter.parse), so no gate
            # and Bilingua gets those same characters
            data = data.decode("utf-8", "replace").encode()
            gated = False
        else:
            gated = enforce_caps and self.fmt in _DECODE_GATES
        label = None
        if enforce_caps:
            self._check_input(data)
            if gated:
                try:
                    label = decode_gate(self.fmt, data, kwargs)  # the Python filter's refusal
                except ValueError as exc:
                    _coded(exc, "parse")  # every adapter refusal carries an engine code (5.6)
                    raise
        self._memo = None
        try:
            doc, locos, memo_key = self._extract_units(
                bl,
                data,
                memo="keep",
                source_lang=source_lang,
                enforce_caps=enforce_caps,
                options=options,
            )
        except (BilinguaParseError, InputLimitError) as exc:
            if self.fmt in _REFUSAL_BY_PYTHON and enforce_caps and self.stored is None:
                self._python_refusal(data, kwargs, exc)
            if not isinstance(exc, BilinguaParseError):
                raise
            refusal = _import_refusal(self.fmt, exc)
            if refusal is exc:
                raise
            raise refusal from exc
        if gated and self.stored is None and _other_code_page(label, doc.source_encoding):
            # decision 22 (#509): the upload gate read this legacy file in LocOS's code page
            # (the project's source language), Bilingua read it in another one (FINAL2:
            # ini, yaml and js ignore the source language), so its text would be garbled.
            # The Python filter imports it instead: a Python document, rebuilt by Python.
            _LOG.info(
                "bilingua %s: read as %s, LocOS reads it as %s; imported by the Python filter",
                self.fmt,
                _codec(doc.source_encoding),
                label,
            )
            return self._python_parse(data, provider, kwargs)
        if enforce_caps:
            check_segment_cap(len(locos))
            check_unit_texts([u["text"] for u in locos])
        if not locos:
            # as the Python filters: zero units is a parse result (ingest refuses it, #573),
            # unless the Python filter refuses the file itself with its own sentence
            empty = _empty_refusal(self.fmt, options)
            if empty:
                raise EmptyDocumentError(empty)
            if self.fmt in _EMPTY_BY_PYTHON and enforce_caps:
                # the Python filter tells why (its own class and sentence) and raises; only
                # on this rare path is the file parsed a second time
                _python_filter(self.fmt).parse(data)
            if self.fmt in _REFUSAL_BY_PYTHON and enforce_caps and self.stored is None:
                # an Office file the Python filter refuses (no workbook, column mapping that
                # fits no sheet) may read as zero units here: its refusal, as for a refusal
                self._python_refusal(data, kwargs, None)
        units = [
            TextUnit(
                p_index=u["p_index"],
                text=u["text"],
                style=u.get("style") or "",
                inline_map=u.get("map") or {},
            )
            for u in locos
        ]
        parser = doc.parser or {}
        stamp = {
            "format": parser.get("format") or doc.format,
            "crate": parser.get("crate"),
            "version": parser.get("crate_version"),
            "schema": parser.get("schema"),
            "wheel": parser.get("bilingua") or bl.version(),
            "profile": {"name": parser.get("profile"), "version": parser.get("profile_version")},
            "source_lang": source_lang,
            "segment_ids": [u["segment_id"] for u in locos],
            "units_sha256": _units_sha(locos),
            "original_sha256": hashlib.sha256(data).hexdigest(),
        }
        pidx = [u.p_index for u in units]
        if pidx != list(range(len(pidx))):
            stamp["p_indexes"] = pidx
        stamped = {k: v for k, v in options.items() if k != _HH_UTF8_FALLBACK}
        if stamped:
            stamp["options"] = stamped
        skeleton = {
            "format": self.fmt,
            "units": [
                {"p_index": u.p_index, "text": u.text, "style": u.style or ""} for u in units
            ],
            SKELETON_KEY: stamp,
        }
        comment = _COMMENT_OPTION.get(self.fmt)
        if comment and not (self.fmt == "xlsx" and options.get("xlsx.columns")):
            # the #690 record the Python filters keep: "comments": True when comment units were
            # offered, and DOCX (on by default) "comments": False when they were left out
            on = bool(options.get(comment[0], comment[1]))
            if on != comment[1]:
                skeleton["comments"] = on
        if self.fmt == "xlsx" and options.get("xlsx.columns"):
            # column mode: the note columns of each row, as XlsxFilter records unit["note"]
            # (tabular.unit_note); ingest makes them segment notes
            notes = _notes_of(doc)
            for u, row in zip(locos, skeleton["units"], strict=True):
                if notes.get(u["segment_id"]):
                    row["note"] = notes[u["segment_id"]]
        if self.fmt in _UNIT_NOTES:
            # ITS locNote (xml_its), XMB/XTB meaning and description (angular_xmb): the
            # unit's note, as those filters record unit["note"]; ingest makes segment notes
            notes = _notes_of(doc)
            for u, row in zip(locos, skeleton["units"], strict=True):
                if notes.get(u["segment_id"]):
                    row["note"] = notes[u["segment_id"]]
        images = a11y_images(doc, locos)
        if images:
            skeleton["a11y_images"] = images  # ingest turns it into the missing-alt prompts
        if self.fmt == "json":
            # the resource dialect the wheel read the file as, recorded as the Python filter
            # records it (absent for generic JSON); export hands it back as `dialect`
            dialect = _json_dialect(doc)
            if dialect:
                skeleton["dialect"] = dialect
        if self.fmt in ("md", "mdx"):
            # the allowlist this import used, as the Python filter records it (#731 gate): export
            # hands it back as fm_keys, the same list the stamp's options already carry
            skeleton["md_fm_keys"] = _md_fm_keys(options)
        self._note_metadata(skeleton, doc, data)
        if self.fmt in _XLIFF_PACKAGES:
            skeleton["package"] = _package_listing(doc.to_dict(), locos, data)
        self._note_encoding(skeleton, doc, data, source_lang, hint, gated, label)
        cp = options.get("htmlhelp.code_page")
        if gated and isinstance(cp, int) and label:
            # as HhcFilter.parse: the project code page is stamped when it decided the decode
            # (ingest's repair and export hand it back as hhp_code_page)
            from app.filters.htmlhelp_filter import _hint_codec

            if label == _hint_codec(cp):
                skeleton["hhp_code_page"] = cp
        self._memo = (memo_key, doc, locos)  # last: a refused parse leaves no memo
        return ParseResult(skeleton=skeleton, units=units)

    def _unstamped_build_options(self, options: dict, paragraph_texts: dict) -> dict:
        """A build with no stamped skeleton has no import options to replay (an export always
        has them). As the Python filters' build, which writes every unit it is given, a
        mapping that addresses comment units (p_index from office_comments.COMMENT_BASE) turns
        the format's comment option on, so those units exist to be written."""
        comment = _COMMENT_OPTION.get(self.fmt)
        if not comment or comment[1] or comment[0] in options:
            return options
        from app.filters.office_comments import COMMENT_BASE

        def addresses_comment(key) -> bool:
            try:
                return int(key) >= COMMENT_BASE
            except (TypeError, ValueError):
                return False

        if any(addresses_comment(k) for k in paragraph_texts):
            return {**options, comment[0]: True}
        return options

    def _python_parse(self, data: bytes, provider, kwargs: dict) -> ParseResult:
        """The Python filter's parse of a new upload the adapter does not import (see the
        decision 22 check in parse), with the parse keywords that filter takes."""
        import inspect

        self._memo = None
        py = _python_filter(self.fmt)
        params = inspect.signature(py.parse).parameters
        pykw = {k: v for k, v in kwargs.items() if k in params}
        return py.parse(data, provider, **pykw)

    def _python_refusal(self, data: bytes, kwargs: dict, exc: Exception) -> None:
        """Wave 4 (see _REFUSAL_BY_PYTHON): Bilingua refused a new import; when the Python
        filter refuses it too, its refusal (class and sentence) is raised instead. Returns
        when the Python filter reads the file: Bilingua's refusal then stands."""
        import inspect

        py = _python_filter(self.fmt)
        params = inspect.signature(py.parse).parameters
        pykw = {k: v for k, v in kwargs.items() if k in params}
        try:
            py.parse(data, **pykw)
        except ValueError as pexc:  # the Python filters refuse with a ValueError (or subclass)
            if not hasattr(pexc, "code"):
                # the engine's error code rides along, as on every adapter refusal (plan 5.6)
                pexc.code = getattr(exc, "code", None) or "parse"
            raise pexc from exc
        except Exception:  # a Python crash is no refusal: Bilingua's stands
            _LOG.info("bilingua %s: the Python filter failed on a file Bilingua refused", self.fmt)

    def _python_build(self, original, paragraph_texts, target_locale, kwargs) -> bytes | None:
        """The Python filter's build of an unstamped original Bilingua cannot read (see
        _BUILD_BY_PYTHON); None when the Python writer refuses or fails too."""
        import inspect

        py = _python_filter(self.fmt)
        fn = py.build_localized if target_locale else py.build
        params = inspect.signature(fn).parameters
        pykw = {k: v for k, v in kwargs.items() if k == "fallback_keys" and k in params}
        try:
            if target_locale:
                return fn(bytes(original), paragraph_texts, target_locale, **pykw)
            return fn(bytes(original), paragraph_texts, **pykw)
        except Exception:
            _LOG.info("bilingua %s: the Python writer cannot build the stored original", self.fmt)
            return None

    def _python_write_refusal(self, original, paragraph_texts, target_locale, kwargs, exc):
        """Wave 6 (see _WRITE_REFUSAL_BY_PYTHON): Bilingua cannot write a translation into the
        file (a cue that would drop out or read back differently, a character the caption
        format cannot show, text that does not fit); when the Python writer refuses the same
        translations, its refusal (class and sentence, e.g. CaptionTargetError "Cue 3 was not
        exported: ...", which export shows as such) is raised instead. The Python output is
        never used. When the Python writer writes the file, Bilingua's refusal stands."""
        import inspect

        py = _python_filter("js" if self.fmt == _QT_TS else self.fmt)
        fn = py.build_localized if target_locale else py.build
        params = inspect.signature(fn).parameters
        pykw = {k: v for k, v in kwargs.items() if k == "fallback_keys" and k in params}
        if self.fmt in _HTMLHELP:
            pykw.update(self._htmlhelp_write_kwargs(kwargs, params))
        try:
            if target_locale:
                fn(bytes(original), paragraph_texts, target_locale, **pykw)
            else:
                fn(bytes(original), paragraph_texts, **pykw)
        except InputLimitError as pexc:
            _coded(pexc, "limit")
            raise pexc from exc  # export keeps a limit the 413 it is
        except ValueError as pexc:
            raise _write_refusal(pexc) from exc
        except Exception:  # a Python crash is no refusal: Bilingua's stands
            _LOG.info("bilingua %s: the Python writer failed where Bilingua refused", self.fmt)

    def _htmlhelp_write_kwargs(self, kwargs: dict, params) -> dict:
        """The HTML Help writer's reading of the file, as the Bilingua build read it: the project
        code page (the stamped `htmlhelp.code_page`, else the caller's `hhp_code_page`) and the
        decode version. A Bilingua document reads as decode version 2 (the wheel's HTML Help
        decode is LocOS's v2, W5 B); it carries no Python `hh_decode` stamp, so export's
        version 1 for an unstamped document is not used for it."""
        from app.filters.htmlhelp_filter import HH_DECODE_VERSION

        out: dict = {}
        if "hhp_code_page" in params:
            cp = self._options(kwargs).get("htmlhelp.code_page")
            if isinstance(cp, int):
                out["hhp_code_page"] = cp
        if "hh_decode" in params:
            if self.stored is not None:
                out["hh_decode"] = HH_DECODE_VERSION
            elif kwargs.get("hh_decode") is not None:
                out["hh_decode"] = kwargs["hh_decode"]
        return out

    def _note_metadata(self, skeleton: dict, doc: dict, data: bytes) -> None:
        """The skeleton keys the Python filter of a wave-2 format records beside its units."""
        if self.fmt == "html":
            server = self._html_dialect(data)
            if server is not None:
                skeleton["server_code"] = server  # as html_filter records it (#672)
        elif self.fmt == "arb":
            skeleton["dialect"] = "arb"  # json_filter.ArbFilter: dialect_parse(.., "arb")
        elif self.fmt == "yaml":
            rails = _unit_extra(doc, "rails_locale")  # the Rails root key (yaml_filter)
            if isinstance(rails, str) and rails:
                skeleton["rails_locale"] = rails
        elif self.fmt == "xcstrings":
            lang = _xcstrings_source_language(data)
            if lang is not None:
                skeleton["source_language"] = lang
        elif self.fmt == "xcloc":
            skeleton.update(_xcloc_meta(data))
        elif self.fmt == "sdlrpx":
            meta = _sdl_package_meta(data)
            if meta is not None:
                skeleton["sdl_package"] = meta  # services/sdl_package reads and extends it

    def _note_encoding(
        self,
        skeleton: dict,
        doc: dict,
        data: bytes,
        source_lang,
        hint,
        gated: bool = False,
        label: str | None = None,
    ) -> None:
        """file_warnings, source_encoding and text_code_page, the way the Python filters and
        ingest's apply_code_page_hint record them. A gated format (decode gate ran) records
        the gate's label, and the gate already set the hint's code page and encoding warning
        as the Python filter does: Bilingua's own encoding warnings are not added again.
        Ingest does not apply the hint to a Bilingua skeleton, so the hint's encoding warnings
        (decision 22: letters that look wrong, a Western file read as Windows-1252) are put on
        file_warnings here (FINAL2: they were lost)."""
        if gated:
            if label and label != "utf-8":
                skeleton["source_encoding"] = label
            warnings = [
                {k: w[k] for k in ("kind", "part", "message") if w.get(k) is not None}
                for w in doc.warnings or []
                if isinstance(w, dict) and w.get("kind") not in ("text_encoding", _MEMBER_SLOTS)
            ]
            if self.fmt == "ttx":
                for w in [w for w in warnings if w.get("kind") == _TTX_UNSEGMENTED]:
                    warnings.remove(w)
                    m = _RUNS_RE.match(w.get("message") or "")
                    if m and int(m.group(1)):
                        skeleton["unsegmented_text"] = int(m.group(1))
            if self.fmt == "docx":
                warnings = _docx_file_warnings(warnings, data)
            _put_warnings(skeleton, warnings, hint)
            return
        enc = _codec(doc.source_encoding) or _fallback_encoding(data, source_lang)
        if enc and enc not in ("utf-8", "utf_8", "utf-8-sig"):
            skeleton["source_encoding"] = enc
        legacy = _hint_code_page(enc)
        if legacy:
            skeleton["text_code_page"] = legacy
            if hint is not None:
                hint.used = legacy  # ingest's apply_code_page_hint keeps the stamp
        warnings = []
        for w in doc.warnings or []:
            if not isinstance(w, dict) or w.get("kind") == _MEMBER_SLOTS:
                continue
            w = {k: w[k] for k in ("kind", "part", "message") if w.get(k) is not None}
            if w.get("kind") == "text_encoding" and hint is not None:
                if w not in hint.warnings:
                    hint.warnings.append(w)
                continue
            warnings.append(w)
        _put_warnings(skeleton, warnings, hint)

    def build(self, original: bytes, paragraph_texts: dict, **kwargs) -> bytes:
        other = self._handoff(bytes(original))
        if other is not None:
            return other.build(bytes(original), paragraph_texts)
        return self._build(original, paragraph_texts, None, kwargs)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None, **kwargs
    ) -> bytes:
        other = self._handoff(bytes(original))
        if other is not None:
            return other.build_localized(bytes(original), paragraph_texts, target_locale)
        return self._build(original, paragraph_texts, target_locale, kwargs)

    def _check_stamp(self, original: bytes, doc, locos: list[dict]) -> None:
        """The stored stamp against a fresh extraction (a DocumentHandle) of the stored
        original (plan 2.4)."""
        st = self.stored
        want = st.get("original_sha256")
        if want and hashlib.sha256(original).hexdigest() != want:
            raise BilinguaParseError(
                "The stored original is not the file this document was imported from; " + _REIMPORT,
                "invalid_document",
            )
        parser = doc.parser or {}
        prof = st.get("profile") if isinstance(st.get("profile"), dict) else {}
        now = {
            "profile name": (prof.get("name"), parser.get("profile")),
            "profile version": (prof.get("version"), parser.get("profile_version")),
            "schema": (st.get("schema"), parser.get("schema")),
        }
        for what, (have, got) in now.items():
            if have != got:
                raise BilinguaParserMismatch(
                    f"This document was imported with Bilingua {what} {have!r}; this install "
                    f"reads it with {got!r}. {_REIMPORT}",
                    "parser_version",
                )
        if self._stored_units:
            # as export's HTML check for the Python filter: stored indices must never address
            # a different extraction (a prefix, since older imports may omit trailing attrs)
            old = [(u.get("p_index"), u.get("text")) for u in self._stored_units]
            new = [(u["p_index"], u["text"]) for u in locos][: len(old)]
            if old != new:
                raise BilinguaParserMismatch(
                    "HTML extraction has changed. Re-import the original HTML and review its "
                    "translations before exporting.",
                    "parser_version",
                )
        ids = [u["segment_id"] for u in locos]
        same = (st.get("segment_ids") or []) == ids and (
            not st.get("units_sha256") or st["units_sha256"] == _units_sha(locos)
        )
        if not same:
            raise BilinguaParserMismatch(
                f"This document was imported with Bilingua {st.get('wheel')} "
                f"({st.get('crate')} {st.get('version')}); this install "
                f"({parser.get('bilingua')}, {parser.get('crate')} {parser.get('crate_version')}) "
                f"reads it differently. {_REIMPORT}",
                "parser_version",
            )

    def _build(self, original, paragraph_texts, target_locale, kwargs) -> bytes:
        bl = _module()
        if bl is None:
            raise BilinguaUnavailable("The bilingua module is not installed.")
        original = bytes(original)
        options = self._options(kwargs)
        if self.stored is not None:
            source_lang = self.stored.get("source_lang")
        else:
            hint = _code_page_hint()
            source_lang = (getattr(hint, "locale", "") or None) if hint is not None else None
        if self.stored is None:
            options = self._unstamped_build_options(options, paragraph_texts)
            if self.fmt in ("csv", "tsv") and "csv.columns" not in options:
                # a build with no stamp that hands the Python column plan (`table=`, as the
                # tabular filter's callers do): the same columns as the Bilingua mapping
                cols = _csv_plan_columns(kwargs.get("table"))
                if cols:
                    options = {**options, "csv.columns": cols}
        try:
            doc, locos, _key = self._extract_units(
                bl,
                original,
                memo="take",
                source_lang=source_lang,
                target_lang=target_locale,
                options=options,
            )
        except (BilinguaParseError, InputLimitError) as exc:
            # a build with no stamped skeleton (never an export: tools and the parity runner)
            # refuses as the Python filter's build would (see _REFUSAL_BY_PYTHON)
            if self.stored is None and self.fmt in _REFUSAL_BY_PYTHON:
                self._python_refusal(original, {}, exc)
            if (
                self.stored is None
                and self.fmt in _BUILD_BY_PYTHON
                and isinstance(exc, BilinguaParseError)
            ):
                # a stored pre-#509 original that is no longer readable keeps its bytes in the
                # Python writer while its parse refuses (FINAL3 I4): LocOS builds it, so does
                # the adapter, by the Python filter; a Python refusal or crash leaves Bilingua's
                built = self._python_build(original, paragraph_texts, target_locale, kwargs)
                if built is not None:
                    return built
            raise
        if self.stored is not None:
            self._check_stamp(original, doc, locos)
        skip = set(map(str, kwargs.get("fallback_keys") or ()))
        targets: dict[str, str] = {}
        missing = object()
        for u in locos:
            key = str(u["p_index"])
            if key in skip:
                continue
            val = paragraph_texts.get(key, missing)
            if val is missing:
                val = paragraph_texts.get(u["p_index"], missing)
            if val is missing or val is None:
                continue
            val = str(val)
            if self.fmt in _XML_TARGET or (
                self.fmt in _XML_PLIST and not original.startswith(b"bplist")
            ):
                from app.filters.xml_text import xml_target_text

                val = xml_target_text(val)  # as the Python filter, before it tokenizes
            targets[u["segment_id"]] = val
        self.last_problems = {}
        self.last_qa_flags = {}
        untranslated = doc  # the export guard's reference (see _BUILD_GUARDS)
        if targets:
            try:
                doc, problems = _call(bl, self.fmt, doc.locos_set_targets, targets)
            except BilinguaWriteRefused as exc:
                if self.fmt in _WRITE_REFUSAL_BY_PYTHON and exc.code == "unwritable":
                    self._python_write_refusal(
                        original, paragraph_texts, target_locale, kwargs, exc
                    )
                raise
            self.last_problems = {k: v for k, v in (problems or {}).items() if v}
            self.last_qa_flags = _repair_flags(locos, targets, self.last_problems)
            kept = sum(
                1
                for v in self.last_problems.values()
                if any(p.get("kind") not in REPAIR_KINDS for p in v)
            )
            if kept:
                _LOG.info("bilingua %s: tokens kept as text in %d segment(s)", self.fmt, kept)
            if self.last_qa_flags:
                _LOG.info(
                    "bilingua %s: inline tags repaired in %d segment(s)",
                    self.fmt,
                    len(self.last_qa_flags),
                )
        mkw: dict = {"profile": PROFILE}
        pv = self._profile_version()
        if pv is not None:
            mkw["profile_version"] = pv
        if options:
            mkw["format_options"] = dict(options)
        _trace("merge", self.fmt)
        try:
            out = _call(bl, self.fmt, doc.merge, **mkw)
        except BilinguaWriteRefused as exc:
            if self.fmt in _WRITE_REFUSAL_BY_PYTHON and exc.code == "unwritable":
                self._python_write_refusal(original, paragraph_texts, target_locale, kwargs, exc)
            raise
        guard = _BUILD_GUARDS.get(self.fmt)
        if guard is not None and targets and out != original:
            # wave 5: the Python writers' export checks on what Bilingua wrote, against the
            # same build without translations (a localized one keeps its language declaration)
            reference = original
            if target_locale:
                try:
                    reference = _call(bl, self.fmt, untranslated.merge, **mkw)
                except BilinguaWriteRefused:
                    # the same build without translations can be one LocOS refuses itself (a
                    # Cyrillic .hhk localized to en-US with nothing translated:
                    # HtmlHelpEncodingError); the guard then compares with the stored original
                    reference = original
            try:
                guard(reference, out, target_locale)
            except ValueError as exc:
                _coded(exc, "write")
                raise
        return out


_REFUSAL_CLASSES: dict = {}


def _write_refusal(pexc: ValueError) -> Exception:
    """The Python writer's refusal (wave 6, _python_write_refusal) as an adapter write refusal:
    an instance of BOTH BilinguaWriteRefused, which export shows with its own sentence for a
    Bilingua document ("Cue 3 was not exported: ...", not the generic "could not be rebuilt"),
    and the Python class (CaptionTargetError, ...), which the filter's callers catch; same
    sentence, engine code `unwritable`."""
    cls = type(pexc)
    if isinstance(pexc, BilinguaWriteRefused):
        return pexc
    both = _REFUSAL_CLASSES.get(cls)
    if both is None:
        both = type("Bilingua" + cls.__name__, (BilinguaWriteRefused, cls), {})
        both.__module__ = __name__
        _REFUSAL_CLASSES[cls] = both
    return both(str(pexc), "unwritable")


# The CAT tag-repair kinds `locos_set_targets` reports since the W3 wheel (B8): for xliff,
# sdlxliff, mqxliff, mxliff, txlf, txml, ttx and the xlz/mqxlz/sdlppx members the target keeps
# a repeated, stray-close, unclosed or pair-as-point token and the writer repairs it as
# cat_xliff._render / xliff_filter._rebuild_target do (dropped, ignored, closed at the end,
# written empty). Every other kind is a token kept as text (#814).
REPAIR_KINDS = ("repeated", "stray_close", "unclosed", "pair_as_point")


def _repair_flags(locos: list[dict], targets: dict, problems: dict) -> dict:
    """The QA flag LocOS shows for a target its CAT writers repair: LocOS's writers repair
    silently and QA reports the target's tag damage on the segment (quality.tag_damage, a
    `tag_integrity` flag made by quality.tag_flag, the badge the editor shows and the export
    gate reads). The same flag, from the same predicate on the same texts, per p_index of
    each segment the engine repaired. The engine's own list words the reason only when the
    predicate finds nothing (it should not: every repair kind is a token divergence)."""
    repaired = {
        sid: [p for p in probs if p.get("kind") in REPAIR_KINDS] for sid, probs in problems.items()
    }
    repaired = {sid: probs for sid, probs in repaired.items() if probs}
    if not repaired:
        return {}
    from app.services.quality import tag_damage, tag_flag

    flags: dict = {}
    for u in locos:
        probs = repaired.get(u["segment_id"])
        if not probs:
            continue
        reason = tag_damage(u["text"], targets.get(u["segment_id"])) or (
            "inline tag repaired: "
            + ", ".join(f"{p['kind']} {p.get('token', '')}".strip() for p in probs)
        )
        flags[str(u["p_index"])] = [tag_flag(reason)]
    return flags


def _parser_info(doc) -> dict:
    """What made a Document (a DocumentHandle): the `parser` record extract adds (covers
    binary skeletons)."""
    return dict(doc.parser or {})


def _python_filter(fmt: str):
    """The Python filter for `fmt` (bypassing the switch). `ts` is the Qt Linguist filter,
    registered under that name although the `.ts` extension resolves to the js filter."""
    if fmt == _QT_TS:
        get_filter("xml", _python=True)  # app.filters.xml_resource registers QtTsFilter
        from app.filters import base

        return base._FILTERS.get(_QT_TS)
    return get_filter(fmt, _python=True)


__all__ = [
    "ENV_VAR",
    "BilinguaEncodingError",
    "BilinguaFilter",
    "BilinguaModuleRefused",
    "BilinguaParseError",
    "BilinguaParserMismatch",
    "BilinguaUnavailable",
    "BilinguaWriteRefused",
    "EmptyDocumentError",
    "InputLimitError",
    "available",
    "check_input",
    "decode_gate",
    "enabled_formats",
    "inlines_to_locos",
    "is_bilingua_skeleton",
    "limits_from_config",
    "locos_to_inlines",
    "options_from_kwargs",
    "profile_stamp",
    "select",
]
