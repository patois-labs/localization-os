"""MadCap Flare project filter (connector parity wave 4, route C).

A Flare project travels as a zip: Flare's own "zip project" writes a .flprjzip (a plain .zip
of the project folder is accepted once renamed to .flprjzip; .zip itself is NOT registered, so
every other zip upload and connector attachment keeps its "unsupported format" answer). One
upload = one Document; its units are the
translatable text of every file below, numbered in zip member order:

  * topics and snippets (.htm .html .xhtml .flsnp): XHTML. Each block element is ONE unit
    through the HTML filter's tokenizer and unit walker, with the MadCap phrasing elements
    (MadCap:variable, snippetText, conditionalText, xref, glossaryTerm, keyword, ...) added
    to its inline set so they become protected placeholder tokens INSIDE the sentence.
    A MadCap:variable reference is always a point token: its name, and any cached content
    between its tags, is never offered for translation. Block elements (snippetBlock,
    dropDown/dropDownHead/dropDownHotspot/dropDownBody, ...) are unit boundaries, so a
    drop-down hotspot is its own unit. Image alt/title text and link titles follow (the
    HTML filter's a11y units), then the index keywords of MadCap:keyword term="..."
    (one unit per keyword or sub-keyword level; ";" separates keywords, ":" levels).
  * variable sets (.flvar): the text of each <Variable> definition (date/time and other
    typed variables are skipped); a matching EvaluatedDefinition attribute is updated too.
  * TOCs (.fltoc): the Title attribute of each <TocEntry>; [%=System.LinkedTitle%] style
    tokens are protected point tokens and an entry that is only such a token is skipped.
  * glossaries (.flglo): <Term> and plain-text <Definition> content.

Everything else (the .flprj, targets, condition tag sets .flcts, page layouts, stylesheets,
images, skins) is copied byte-for-byte; so is every attribute (MadCap:conditions included)
and every member's name, order, timestamp and compression type. build() of the original
with no changed unit returns the ORIGINAL BYTES; a changed member is rewritten in place
and every other member keeps identical content.

Security: zip_guard (bomb, member count, forged sizes) runs before anything is read;
paths with "..", absolute or drive paths, duplicates (case-insensitive) and symlinks are
refused; XML is never handed to an entity-resolving parser (the HTML tokenizer only
splits text), DTD/ENTITY declarations are refused, and the structured XML parts are
checked well-formed with lxml (no entities, no network, no DTD) before extraction.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import html as _html
import html.entities as _html_entities
import io
import re
import stat
import zipfile

from app import config
from app.filters import a11y, zip_guard
from app.filters import html_filter as _hf
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    register,
)
from app.filters.html_guard import check_build, strict_escape
from app.filters.xml_text import xml_target_text
from app.services.segment import _PLACEHOLDER_RE, ph_point, strip_placeholders

_LABEL = "Flare project"
_BOM = b"\xef\xbb\xbf"

_TOPIC_EXTS = (".htm", ".html", ".xhtml", ".flsnp")
_XML_KINDS = {".flvar": "variables", ".fltoc": "toc", ".flglo": "glossary"}

# MadCap phrasing elements that live INSIDE a sentence (lowercased, as _parse_tag gives).
_MADCAP_INLINE = frozenset(
    {
        "madcap:conditionaltext",
        "madcap:xref",
        "madcap:glossaryterm",
        "madcap:annotation",
        "madcap:change",
        "madcap:toggler",
        "madcap:textpopup",
        "madcap:textpopuphead",
        "madcap:textpopupbody",
        "madcap:expanding",
        "madcap:expandinghead",
        "madcap:expandingbody",
        "madcap:popup",
        "madcap:popuphead",
        "madcap:popupbody",
    }
)
# Point (never-translated, no content offered) MadCap elements.
_MADCAP_VOID = frozenset(
    {"madcap:variable", "madcap:snippettext", "madcap:keyword", "madcap:concept"}
)
FLARE_INLINE = _hf._INLINE_TAGS | _MADCAP_INLINE | _MADCAP_VOID
FLARE_VOID = _hf._VOID_INLINE | _MADCAP_VOID

_KW_SPLIT = re.compile(r"(;|(?<!\\):)")
_FLARE_TOKEN_RE = re.compile(r"\[%=[^%\]]{0,200}%\]")
_DTD_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)

STYLE_KEYWORD = "flare:index-keyword"
STYLE_VARIABLE = "flare:variable"
STYLE_TOC = "flare:toc-title"
STYLE_TERM = "flare:glossary-term"
STYLE_DEFINITION = "flare:glossary-definition"


def _refuse(msg: str) -> InputLimitError:
    return InputLimitError(f"This {_LABEL} {msg} It was not processed.")


def _shown(name: str) -> str:
    return name.encode("ascii", "replace").decode("ascii")[:120]


# --------------------------------------------------------------------------- zip layer
def _open(data: bytes) -> zipfile.ZipFile:
    zip_guard.check(bytes(data), _LABEL)
    try:
        return zipfile.ZipFile(io.BytesIO(bytes(data)))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise InputLimitError(
            "This file is not a zip archive. Upload a MadCap Flare project as a .flprjzip "
            "file (a .zip of the project folder renamed to .flprjzip also works)."
        ) from exc


def _kind(name: str) -> str | None:
    low = name.replace("\\", "/").lower()
    if low.endswith("/") or low.startswith("__macosx/") or low.rsplit("/", 1)[-1].startswith("._"):
        return None
    if low.endswith(_TOPIC_EXTS):
        return "topic"
    for ext, kind in _XML_KINDS.items():
        if low.endswith(ext):
            return kind
    return None


def _members(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str | None]]:
    """Validate every member path, then classify. Refuses traversal, absolute and drive
    paths, NUL bytes, case-insensitive duplicates and symlinks."""
    seen: set[str] = set()
    out = []
    for info in zf.infolist():
        name = info.filename
        norm = name.replace("\\", "/")
        parts = norm.split("/")
        if "\x00" in name or norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or ".." in parts:
            raise _refuse(f"contains an unsafe path ({_shown(name)}).")
        key = norm.rstrip("/").lower()
        if key in seen:
            raise _refuse(f"contains the same path twice ({_shown(name)}).")
        seen.add(key)
        mode = (info.external_attr >> 16) & 0xFFFF
        if info.create_system == 3 and stat.S_ISLNK(mode):
            raise _refuse(f"contains a symbolic link ({_shown(name)}).")
        out.append((info, _kind(name)))
    return out


def _decode(raw: bytes, name: str) -> tuple[str, bool]:
    bom = raw.startswith(_BOM)
    try:
        return raw[3 if bom else 0 :].decode("utf-8"), bom
    except UnicodeDecodeError as exc:
        raise _refuse(f"file {_shown(name)} is not UTF-8 (Flare saves UTF-8).") from exc


def _is_flare(zf: zipfile.ZipFile, members) -> bool:
    for info, kind in members:
        low = info.filename.lower()
        if low.endswith(".flprj") or kind in ("variables", "toc", "glossary"):
            return True
        if low.endswith(".flsnp"):
            return True
    for info, kind in members:
        if kind == "topic" and b"xmlns:madcap" in zf.read(info)[:4096].lower():
            return True
    return False


def _check_xml(text: str, name: str, structured: bool) -> None:
    if _DTD_RE.search(text) and (structured or re.search(r"<!\s*ENTITY\b", text, re.I)):
        raise _refuse(f"file {_shown(name)} declares a DTD or entities.")
    if structured:
        from lxml import etree

        parser = etree.XMLParser(
            resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
        )
        try:
            etree.fromstring(text.encode("utf-8"), parser)
        except etree.XMLSyntaxError as exc:
            raise _refuse(f"file {_shown(name)} is not well-formed XML.") from exc


# --------------------------------------------------------------------------- helpers
# #676: (element, attribute) values this filter translates, beyond html_guard's own list
_FREE_IN_FLARE = frozenset(
    (
        ("madcap:keyword", "term"),
        ("keyword", "term"),  # the same attribute as an XML reader names it
        ("variable", "evaluateddefinition"),
        ("tocentry", "title"),
    )
)


def _xml_escape(text: str, quote: bool = False) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote:
        text = text.replace('"', "&quot;").replace("'", "&apos;")
    return text


_NAMED_REF_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
_XML_PREDEFINED = frozenset({"amp", "lt", "gt", "quot", "apos"})


def _xml_named_refs(text: str) -> str:
    """Topics are XHTML parsed as XML (no DTD), so an HTML-only named reference such as
    &nbsp; in a translation would make the topic invalid. Rewrite each one as numeric
    character references; an unknown name becomes literal text (&amp;name;)."""

    def _sub(m: re.Match) -> str:
        name = m.group(1)
        if name in _XML_PREDEFINED:
            return m.group(0)
        chars = _html_entities.html5.get(name + ";")
        if chars is None:
            return "&amp;" + name + ";"
        return "".join(f"&#{ord(ch)};" for ch in chars)

    return _NAMED_REF_RE.sub(_sub, text)


def _clean(new: str) -> str:
    """A target for a plain-text slot: no placeholder noise, XML 1.0 characters only."""
    return xml_target_text(strip_placeholders(str(new)))


def _has_letter(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def _replace_spans(tok: str, spans: list[tuple[int, int, str]]) -> str:
    for start, end, val in sorted(spans, reverse=True):
        tok = tok[:start] + val + tok[end:]
    return tok


# --------------------------------------------------------------------------- topics
def _collapse_variables(tokens: list[str]) -> None:
    """A non-self-closing <MadCap:variable ...>cached</MadCap:variable> becomes ONE markup
    token, so its cached content is never a unit (variables are never translated in topics)."""
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if _hf._is_tag_token(tok):
            closing, name, self_closing = _hf._parse_tag(tok)
            if name == "madcap:variable" and not closing and not self_closing:
                j = i + 1
                while j < n:
                    if _hf._is_tag_token(tokens[j]):
                        c, nm, _sc = _hf._parse_tag(tokens[j])
                        if c and nm == "madcap:variable":
                            break
                    j += 1
                if j < n:
                    tokens[i] = _hf._MarkupToken("".join(tokens[i : j + 1]))
                    for k in range(i + 1, j + 1):
                        tokens[k] = _hf._TextToken("")
                    i = j
        i += 1


def _topic_tokens(text: str, budget: list[int] | None) -> list[str]:
    tokens = _hf._tokenize(text, None, budget)
    _collapse_variables(tokens)
    return tokens


def _body_units(tokens: list[str]) -> list[dict]:
    return _hf._iter_units(tokens, inline_tags=FLARE_INLINE, void_inline=FLARE_VOID)


def _keyword_units(tokens: list[str]) -> list[dict]:
    out: list[dict] = []
    for ti, name in _hf._iter_start_tags(tokens):
        if name != "madcap:keyword":
            continue
        for attr, start, end, raw in _hf._tag_attrs(tokens[ti]):
            if attr != "term":
                continue
            parts = _KW_SPLIT.split(_html.unescape(raw))
            for pi in range(0, len(parts), 2):
                core = parts[pi].strip().replace("\\:", ":")
                if core and _has_letter(core):
                    out.append({"ti": ti, "span": (start, end), "piece": pi, "text": core})
    return out


def _apply_keyword_units(tokens: list[str], texts: dict, base: int, strict=None) -> None:
    """`strict` (#676): html_filter.attr_strictness of the token stream as it was before
    any value changed; a value it flags is written opaque to every HTML5 tag state."""
    raws: dict[tuple, str] = {}
    edits: dict[tuple, dict[int, str]] = {}
    for j, u in enumerate(_keyword_units(tokens)):
        new = texts.get(str(base + j))
        if new is None or new == u["text"]:
            continue
        key = (u["ti"], u["span"])
        start, end = u["span"]
        raws[key] = tokens[u["ti"]][start:end]
        # ";" would start a new keyword and ":" a new level: keep the translation one entry.
        val = " ".join(_clean(new).split()).replace(";", ",").replace(":", "\\:")
        edits.setdefault(key, {})[u["piece"]] = val
    per_token: dict[int, list] = {}
    for (ti, (start, end)), pieces in edits.items():
        parts = _KW_SPLIT.split(_html.unescape(raws[(ti, (start, end))]))
        for pi, val in pieces.items():
            lead, _core, trail = _hf._split_ws(parts[pi])
            parts[pi] = lead + val + trail
        val = _xml_escape("".join(parts), True)
        if strict is not None and strict(ti, start, end):
            val = strict_escape(val)
        per_token.setdefault(ti, []).append((start, end, val))
    for ti, spans in per_token.items():
        tokens[ti] = _replace_spans(tokens[ti], spans)


def _topic_units(text: str, budget: list[int] | None) -> list[TextUnit]:
    tokens = _topic_tokens(text, budget)
    units = [TextUnit(p_index=0, text=u["core"], inline_map=u["map"]) for u in _body_units(tokens)]
    units += [
        TextUnit(p_index=0, text=a["text"], style=a["style"]) for a in _hf._iter_attr_units(tokens)
    ]
    units += [
        TextUnit(p_index=0, text=k["text"], style=STYLE_KEYWORD) for k in _keyword_units(tokens)
    ]
    return units


def _topic_build(text: str, texts: dict, base: int, lang: tuple | None) -> tuple[str, int]:
    tokens = _topic_tokens(text, None)
    if lang:
        _hf._declare_language(tokens, lang[0], lang[1])
    before = "".join(tokens)
    strict = _hf.attr_strictness(tokens)  # #676: before any value changes length
    body = _body_units(tokens)
    n_a11y = len(_hf._iter_attr_units(tokens))
    n_kw = len(_keyword_units(tokens))
    # Attribute values first (values only, so the body walk keeps its structure and its
    # point tokens carry the translated alt text and keyword terms along).
    _hf._apply_attr_units(tokens, texts, base + len(body))
    _apply_keyword_units(tokens, texts, base + len(body) + n_a11y, strict)
    for k, u in enumerate(_body_units(tokens)):
        new = texts.get(str(base + k))
        if new is None or new == u["core"]:
            continue  # untouched unit: original bytes, no re-escaping
        tokens[u["start"]] = (
            u["lead"]
            + _hf._rematerialize(_xml_named_refs(xml_target_text(str(new))), u["map"])
            + u["trail"]
        )
        for j in range(u["start"] + 1, u["end"] + 1):
            tokens[j] = ""
    out = "".join(tokens)
    check_build(before, out, extra=_FREE_IN_FLARE)  # #676: browser and XML re-parse
    return out, len(body) + n_a11y + n_kw


# --------------------------------------------------------------------------- XML parts
def _text_elements(tokens: list[str], names: tuple[str, ...]) -> list[dict]:
    """Elements in `names` whose content is a single text run (no child elements)."""
    out: list[dict] = []
    for ti in range(len(tokens) - 2):
        tok = tokens[ti]
        if not _hf._is_tag_token(tok):
            continue
        closing, name, self_closing = _hf._parse_tag(tok)
        if closing or self_closing or name not in names:
            continue
        body, close = tokens[ti + 1], tokens[ti + 2]
        if _hf._is_tag_token(body) or not _hf._is_tag_token(close):
            continue
        c, cname, _sc = _hf._parse_tag(close)
        if not c or cname != name:
            continue
        lead, core, trail = _hf._split_ws(body)
        value = _html.unescape(core)
        if not core or not _has_letter(value):
            continue
        out.append({"ti": ti, "name": name, "lead": lead, "trail": trail, "text": value})
    return out


def _attrs(tok: str) -> dict:
    return {a: (s, e, raw) for a, s, e, raw in _hf._tag_attrs(tok)}


def _variable_items(tokens: list[str]) -> list[dict]:
    items = []
    for el in _text_elements(tokens, ("variable",)):
        vtype = _html.unescape(_attrs(tokens[el["ti"]]).get("type", (0, 0, ""))[2]).strip().lower()
        if vtype in ("", "text"):
            items.append(el)
    return items


def _glossary_items(tokens: list[str]) -> list[dict]:
    return _text_elements(tokens, ("term", "definition"))


def _toc_items(tokens: list[str]) -> list[dict]:
    out: list[dict] = []
    for ti, name in _hf._iter_start_tags(tokens):
        if name != "tocentry":
            continue
        for attr, start, end, raw in _hf._tag_attrs(tokens[ti]):
            if attr != "title":
                continue
            value = _html.unescape(raw)
            pmap: dict[str, str] = {}

            def _sub(m, pmap=pmap):
                pmap[str(len(pmap) + 1)] = m.group(0)
                return ph_point("x", len(pmap))

            lead, core, trail = _hf._split_ws(_FLARE_TOKEN_RE.sub(_sub, value))
            if not core or not _has_letter(strip_placeholders(core)):
                continue
            out.append(
                {
                    "ti": ti,
                    "span": (start, end),
                    "lead": lead,
                    "trail": trail,
                    "text": core,
                    "map": pmap,
                }
            )
    return out


def _toc_title(new: str, pmap: dict) -> str:
    out, pos = [], 0
    new = xml_target_text(str(new))
    for m in _PLACEHOLDER_RE.finditer(new):
        out.append(_xml_escape(new[pos : m.start()], True))
        pos = m.end()
        inner = m.group(0)[1:-1]
        if inner.startswith("x"):
            out.append(_xml_escape(pmap.get(inner[1:], ""), True))
    out.append(_xml_escape(new[pos:], True))
    return "".join(out)


_STYLE_BY_NAME = {"variable": STYLE_VARIABLE, "term": STYLE_TERM, "definition": STYLE_DEFINITION}


def _xml_items(kind: str, tokens: list[str]) -> list[dict]:
    if kind == "variables":
        return _variable_items(tokens)
    if kind == "glossary":
        return _glossary_items(tokens)
    return _toc_items(tokens)


def _xml_units(kind: str, text: str) -> list[TextUnit]:
    tokens = _hf._tokenize(text)
    style_of = (lambda it: STYLE_TOC) if kind == "toc" else (lambda it: _STYLE_BY_NAME[it["name"]])
    return [
        TextUnit(p_index=0, text=it["text"], style=style_of(it)) for it in _xml_items(kind, tokens)
    ]


def _xml_build(kind: str, text: str, texts: dict, base: int) -> tuple[str, int]:
    tokens = _hf._tokenize(text)
    items = _xml_items(kind, tokens)
    for j, it in enumerate(items):
        new = texts.get(str(base + j))
        if new is None or new == it["text"]:
            continue
        ti = it["ti"]
        if kind == "toc":
            start, end = it["span"]
            val = it["lead"] + _toc_title(new, it["map"]) + it["trail"]
            tokens[ti] = _replace_spans(tokens[ti], [(start, end, val)])
            continue
        clean = _clean(new)
        tokens[ti + 1] = it["lead"] + _xml_escape(clean) + it["trail"]
        if kind == "variables":
            ev = _attrs(tokens[ti]).get("evaluateddefinition")
            if ev and _html.unescape(ev[2]).strip() == it["text"]:
                tokens[ti] = _replace_spans(tokens[ti], [(ev[0], ev[1], _xml_escape(clean, True))])
    out = "".join(tokens)
    check_build(text, out, extra=_FREE_IN_FLARE)  # #676
    return out, len(items)


# --------------------------------------------------------------------------- filter
@register
class FlareFilter(Filter):
    # #703: raw <, > and & in a target are escaped on export (quality entity gate).
    escapes_raw_text = True
    fmt = "flprjzip"
    exts = ()

    def parse(
        self, data: bytes, provider: str | None = None, *, enforce_caps: bool = True, **_kw
    ) -> ParseResult:
        zf = _open(data)
        with zf:
            members = _members(zf)
            if not _is_flare(zf, members):
                raise InputLimitError(
                    "This zip is not a MadCap Flare project: it has no .flprj project file, "
                    "no Flare snippets, variables, TOCs or glossaries, and no topics with the "
                    "MadCap namespace. It was not processed."
                )
            budget = [100 * config.MAX_DOCUMENT_SEGMENTS] if enforce_caps else None
            markup = 0
            units: list[TextUnit] = []
            files: list[dict] = []
            for info, kind in members:
                if kind is None:
                    continue
                text, _bom = _decode(zf.read(info), info.filename)
                _check_xml(text, info.filename, structured=kind != "topic")
                markup += text.count("<")
                if enforce_caps and markup > 10 * config.MAX_DOCUMENT_SEGMENTS:
                    raise InputLimitError(
                        "This Flare project has far more markup than MAX_DOCUMENT_SEGMENTS allows "
                        f"({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into smaller ones."
                    )
                found = _topic_units(text, budget) if kind == "topic" else _xml_units(kind, text)
                start = len(units)
                for u in found:
                    u.p_index = len(units)
                    units.append(u)
                    if enforce_caps:
                        check_segment_cap(len(units))
                files.append(
                    {"name": info.filename, "kind": kind, "start": start, "count": len(found)}
                )
        skeleton = {
            "format": "flare",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
            "files": files,
        }
        return ParseResult(skeleton=skeleton, units=units)

    def _build(self, original: bytes, paragraph_texts: dict, lang: tuple | None) -> bytes:
        # XML 1.0 characters only in every slot (the shared alt/title writer escapes but
        # does not drop control characters, which would make a topic invalid XML).
        paragraph_texts = {
            k: (v if v is None else xml_target_text(str(v))) for k, v in paragraph_texts.items()
        }
        zf = _open(original)
        with zf:
            members = _members(zf)
            changed: dict[str, bytes] = {}
            base = 0
            for info, kind in members:
                if kind is None:
                    continue
                raw = zf.read(info)
                text, bom = _decode(raw, info.filename)
                if kind == "topic":
                    new_text, count = _topic_build(text, paragraph_texts, base, lang)
                else:
                    new_text, count = _xml_build(kind, text, paragraph_texts, base)
                base += count
                if new_text != text:
                    changed[info.filename] = (_BOM if bom else b"") + new_text.encode("utf-8")
            if not changed:
                return bytes(original)
            return _rezip(zf, changed)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._build(original, paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        tag = a11y.target_tag(target_locale)
        lang = (tag, a11y.target_direction(target_locale)) if tag else None
        return self._build(original, paragraph_texts, lang)


def _rezip(zf: zipfile.ZipFile, changed: dict[str, bytes]) -> bytes:
    """Write a new zip with the same members in the same order, names, timestamps,
    compression types, permissions and comments; only `changed` members get new content."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        out.comment = zf.comment
        for info in zf.infolist():
            data = changed.get(info.filename)
            if data is None:
                data = zf.read(info)
            date_time = info.date_time if info.date_time[0] >= 1980 else (1980, 1, 1, 0, 0, 0)
            zi = zipfile.ZipInfo(info.filename, date_time=date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            zi.comment = info.comment
            out.writestr(zi, data)
    return buf.getvalue()
