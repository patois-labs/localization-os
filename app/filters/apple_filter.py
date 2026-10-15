"""Apple resource filters (file-format Batch 8): `.plist` (Info.plist, Settings.bundle and
generic) and `.stringsdict` (plural and device variants), XML or binary, plus the
`.xcstrings` String Catalog (JSON; see its section below).

Spec: Apple "Information Property List" and "Localizing strings that contain plurals"
(stringsdict file format), the PropertyList-1.0 DTD, and the CoreFoundation binary plist
layout read by the stdlib `plistlib`.

Design
- One tree model for both encodings. An XML plist is scanned by a small strict tokenizer
  that records the exact character span of every `<string>` value, so build() splices the
  translations into the original text: the declaration, DOCTYPE, comments, indentation,
  entity spelling of untouched values and line endings survive byte for byte. The scanner
  is cross-checked against `plistlib.loads` (keys and strings must agree in order), so a
  file the two readers see differently is refused instead of guessed at. A binary plist is
  read with `plistlib` and written back with `plistlib.dumps(fmt=FMT_BINARY,
  sort_keys=False)`; an unchanged build returns the original bytes.
- Which strings translate: `.stringsdict` every string except the
  NSStringFormatSpecTypeKey / NSStringFormatValueTypeKey type tags; Info.plist only the
  localizable keys (bundle display names, copyright, `*UsageDescription` purpose strings,
  document and UT type names, Siri alternative names); a Settings.bundle plist only
  Title, Titles, ShortTitles and FooterText; any other plist every string that is not an
  identifier, number, URL or build variable. A value with no letter or digit outside its
  placeholders is skipped but counted, like `json_filter._is_translatable`.
- Placeholders: printf and Apple specifiers (`%@`, `%1$@`, `%lld`, `%.2f`, `%#@files@`)
  become `⟦xN⟧` point tokens (the services/segment scheme) in the unit text and are put
  back on build from the original value, so the engine cannot damage them.
- build_localized() of a `.stringsdict` adds the plural categories the TARGET language
  needs (CLDR via babel, e.g. Russian few and many) that the source lacks, filled with
  the translated `other` form, so the file is complete for the runtime; the key-based view
  (`records()`, Batch 9) carries the real per-category forms.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import copy
import json
import plistlib
import re
from dataclasses import dataclass, field

from app import config
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    note_source_encoding,
    register,
)
from app.filters.json_filter import check_json_budget
from app.filters.kv_resource import KeyValueRecord  # Batch 1's shared record type
from app.filters.plurals import PLURAL_ORDER
from app.filters.plurals import target_categories as target_plural_categories
from app.services.segment import ph_free

# --- placeholders ---------------------------------------------------------------------

# Apple format specifiers (String Format Specifiers, Foundation): optional positional
# `n$`, flags, width, precision, length modifier, conversion including `@` (object); and
# the stringsdict / String Catalog variable reference `%#@name@`. `%%` is a literal.
APPLE_PH = re.compile(
    r"%(?:\d+\$)?#@[A-Za-z_][A-Za-z0-9_]*@"
    r"|%arg(?![A-Za-z0-9_])"  # String Catalog substitution argument
    r"|%(?:\d+\$)?[-+ #0']*(?:\d+|\*)?(?:\.(?:\d+|\*))?(?:hh|h|ll|l|q|L|z|t|j)?"
    r"[@diouxXcCsSpaAeEfFgGD]"
)
_TOKEN = re.compile(r"⟦[xp](\d+)⟧")  # #693: ⟦pN⟧ = positional, may move
_POSITIONAL = re.compile(r"%\d+\$")


def is_translatable(value: str) -> bool:
    """A letter or digit outside the placeholders (`%#@files@`, `%lld` are skipped)."""
    if not any(c.isalnum() for c in value):
        return False
    return any(c.isalnum() for c in APPLE_PH.sub("", value.replace("%%", "")))


def tokenize(value: str) -> tuple[str, list[str]]:
    """Replace every specifier with ⟦xN⟧ (N from 1 in source order). #693: a positional
    specifier (`%1$@`, `%2$d`, `%1$#@v@`) becomes ⟦pN⟧ instead: it names its argument, so
    the translation may move it (detokenize maps by N); an unnumbered `%@` keeps ⟦xN⟧,
    whose order is the argument order."""
    found: list[str] = []

    def repl(m: re.Match) -> str:
        found.append(m.group(0))
        if _POSITIONAL.match(m.group(0)):
            return ph_free(len(found))
        return f"⟦x{len(found)}⟧"

    pieces = value.split("%%")  # a literal percent is never a specifier start
    return "%%".join(APPLE_PH.sub(repl, p) for p in pieces), found


def detokenize(text: str, tokens: list[str]) -> str:
    """Put the original specifiers back; an unknown token number is dropped (never a
    literal ⟦xN⟧ in the delivered file)."""

    def repl(m: re.Match) -> str:
        n = int(m.group(1))
        return tokens[n - 1] if 1 <= n <= len(tokens) else ""

    return _TOKEN.sub(repl, text)


def split_ws(s: str) -> tuple[str, str, str]:
    core = s.strip()
    if not core:
        return s, "", ""
    return s[: len(s) - len(s.lstrip())], core, s[len(s.rstrip()) :]


def unit_text(value: str) -> tuple[str, str, str, list[str]]:
    """(lead, unit text, trail, tokens) for one translatable value."""
    lead, core, trail = split_ws(value)
    text, tokens = tokenize(core)
    if len(text) > config.MAX_SEGMENT_CHARS:
        raise InputLimitError(
            f"This file has a value longer than {config.MAX_SEGMENT_CHARS} characters "
            "(MAX_SEGMENT_CHARS). Break it into shorter values."
        )
    return lead, text, trail, tokens


def apply_translation(value: str, new: str | None) -> str | None:
    """The new full value for a translated unit, or None when it stays as it is."""
    if new is None:
        return None
    lead, text, trail, tokens = unit_text(value)
    if new == text:
        return None
    return lead + detokenize(new, tokens) + trail


# --- CLDR plural categories -----------------------------------------------------------

# PLURAL_ORDER and target_plural_categories: the rule every plural-aware filter shares
# (decisions 13 and 18), app/filters/plurals.py.


# --- the tree -------------------------------------------------------------------------

_MAX_DEPTH = 128
_SCALARS = {"integer", "real", "true", "false", "date", "data"}


@dataclass
class Node:
    kind: str  # dict | array | string | other
    text: str = ""  # decoded string value (kind == string)
    obj: object = None  # binary: the python value of an `other` leaf
    keys: list = field(default_factory=list)  # dict: key strings, in order
    key_nodes: list = field(default_factory=list)  # dict (XML): the <key> nodes
    children: list = field(default_factory=list)  # dict values / array items
    cstart: int = -1  # XML: content span of a <string>/<key> (-1 = self-closing)
    cend: int = -1
    estart: int = -1  # XML: span of the whole element
    eend: int = -1


class PlistError(ValueError):
    """A plist that cannot be read safely. Caught by the upload route as a polite parse
    refusal, like a JSONDecodeError."""


def check_plist_budget(raw: bytes, what: str = "This property list") -> None:
    """Pre-parse budget: markup characters (XML) bound the element count at C speed;
    refuse far above any real resource file, before any tree exists (#571 pattern)."""
    if raw.count(b"<") > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"{what} has far more entries than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


_XML_TOKEN = re.compile(
    r"<!--.*?-->"
    r"|<!\[CDATA\[(?P<cdata>.*?)\]\]>"
    r"|<\?.*?\?>"
    r"|<!DOCTYPE\s+plist\b[^<>\[\]]*>"
    r"|<(?P<close>/)?(?P<name>[A-Za-z_][\w.:-]*)(?P<attrs>(?:\s[^<>]*?)?)\s*(?P<empty>/)?>",
    re.S,
)
_ENTITY = re.compile(r"&(?:#x([0-9A-Fa-f]{1,6})|#([0-9]{1,7})|(lt|gt|amp|quot|apos));")
_NAMED = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}


def _eol(s: str) -> str:
    """XML end-of-line handling (XML 1.0 section 2.11), as expat and plistlib apply it."""
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _unescape(s: str, at: int) -> str:
    s = _eol(s)

    def repl(m: re.Match) -> str:
        if m.group(3):
            return _NAMED[m.group(3)]
        n = int(m.group(1), 16) if m.group(1) else int(m.group(2))
        try:
            return chr(n)
        except (ValueError, OverflowError):
            raise PlistError(f"invalid character reference at offset {at}") from None

    out = _ENTITY.sub(repl, s)
    if "&" in _ENTITY.sub("", s):
        raise PlistError(
            "property list uses an entity other than &lt; &gt; &amp; &quot; &apos; "
            "(entity declarations are refused)"
        )
    return out


def escape_xml(s: str) -> str:
    from app.filters.xml_text import xml_target_text

    s = xml_target_text(s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _scan_xml(text: str) -> Node:
    """Strict scan of an XML plist into Nodes with spans (no DTD, no custom entities)."""
    if "<!ENTITY" in text or re.search(r"<!DOCTYPE[^>]*\[", text):
        raise PlistError("property list declares an internal DTD or entities; refused")
    stack: list[tuple[str, Node | None, int]] = []  # (element name, node, content start)
    pending_key: list[Node | None] = [None]
    content: list[str] = []
    root: Node | None = None
    pos = 0
    n = len(text)

    def attach(node: Node) -> None:
        nonlocal root
        if not stack:
            raise PlistError("property list has content outside <plist>")
        name, parent, _ = stack[-1]
        if name == "plist":
            if root is not None:
                raise PlistError("<plist> holds more than one value")
            root = node
        elif name == "array":
            parent.children.append(node)
        elif name == "dict":
            if node.kind == "key":
                if pending_key[-1] is not None:
                    raise PlistError("<dict> has a <key> with no value")
                pending_key[-1] = node
            else:
                key = pending_key[-1]
                if key is None:
                    raise PlistError("<dict> has a value with no <key>")
                parent.keys.append(key.text)
                parent.key_nodes.append(key)
                parent.children.append(node)
                pending_key[-1] = None
        else:
            raise PlistError(f"<{name}> cannot contain elements")

    while pos < n:
        lt = text.find("<", pos)
        chunk = text[pos : lt if lt >= 0 else n]
        if chunk:
            if stack and stack[-1][0] in ("string", "key"):
                content.append(_unescape(chunk, pos))
            elif stack and stack[-1][0] in _SCALARS:
                pass  # scalar text (numbers, dates, base64) is validated by plistlib
            elif chunk.strip():
                raise PlistError(f"unexpected text at offset {pos}")
        if lt < 0:
            break
        m = _XML_TOKEN.match(text, lt)
        if not m:
            raise PlistError(f"not a well-formed property list at offset {lt}")
        pos = m.end()
        if m.group("cdata") is not None:
            if not (stack and stack[-1][0] in ("string", "key")):
                if m.group("cdata").strip() and not (stack and stack[-1][0] in _SCALARS):
                    raise PlistError(f"unexpected CDATA at offset {lt}")
                continue
            content.append(_eol(m.group("cdata")))
            continue
        name = m.group("name")
        if name is None:
            continue  # comment, processing instruction, DOCTYPE
        if stack and stack[-1][0] in ("string", "key") and not m.group("close"):
            raise PlistError(f"<{stack[-1][0]}> cannot contain elements")
        if m.group("close"):
            if not stack or stack[-1][0] != name:
                raise PlistError(f"mismatched </{name}> at offset {lt}")
            ename, node, cstart = stack.pop()
            if ename == "plist":
                continue
            if ename == "dict" and pending_key.pop() is not None:
                raise PlistError("<dict> ends with a <key> that has no value")
            if ename in ("string", "key"):
                node.text = "".join(content)
                node.cstart, node.cend = cstart, lt
                content.clear()
            node.eend = pos
            attach(node)
            continue
        if name not in ("plist", "dict", "array", "string", "key") and name not in _SCALARS:
            raise PlistError(f"unknown property list element <{name}>")
        if name == "plist":
            if stack or root is not None:
                raise PlistError("nested <plist>")
            if not m.group("empty"):
                stack.append(("plist", None, pos))
            continue
        if len(stack) > _MAX_DEPTH:
            raise InputLimitError("This property list is nested too deeply.")
        kind = name if name in ("dict", "array", "string", "key") else "other"
        node = Node(kind=kind, estart=lt)
        if m.group("empty"):
            node.eend = pos
            attach(node)
            continue
        stack.append((name, node, pos))
        if name == "dict":
            pending_key.append(None)
        if name in ("string", "key"):
            content.clear()
    if stack:
        raise PlistError("property list ends inside an element")
    if root is None:
        raise PlistError("property list has no value")
    return root


def _tree_from_obj(obj, depth: int = 0, budget: list | None = None) -> Node:
    budget = budget if budget is not None else [0]
    budget[0] += 1
    if budget[0] > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            "This property list has far more entries than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )
    if depth > _MAX_DEPTH:  # also stops a reference cycle in a hostile binary plist
        raise InputLimitError("This property list is nested too deeply.")
    if isinstance(obj, dict):
        node = Node(kind="dict")
        for k, v in obj.items():
            if not isinstance(k, str):
                raise PlistError("property list has a non-string dictionary key")
            node.keys.append(k)
            node.children.append(_tree_from_obj(v, depth + 1, budget))
        return node
    if isinstance(obj, list):
        return Node(kind="array", children=[_tree_from_obj(v, depth + 1, budget) for v in obj])
    if isinstance(obj, str):
        return Node(kind="string", text=obj)
    return Node(kind="other", obj=obj)


def _same_shape(a: Node, b: Node) -> bool:
    """Scanner tree vs plistlib tree: same containers, keys and string values."""
    if a.kind != b.kind:
        return False
    if a.kind == "string":
        return a.text == b.text
    if a.kind == "dict" and a.keys != b.keys:
        return False
    if len(a.children) != len(b.children):
        return False
    return all(_same_shape(x, y) for x, y in zip(a.children, b.children, strict=True))


@dataclass
class PlistDoc:
    root: Node
    binary: bool
    text: str = ""  # XML: decoded text
    bom: bytes = b""
    codec: str = "utf-8"
    edits: dict = field(default_factory=dict)  # id(string node) -> new value
    inserts: list = field(default_factory=list)  # (dict node, before index, key, value)


def read_plist(data: bytes) -> PlistDoc:
    raw = bytes(data)
    check_plist_budget(raw)
    if raw.startswith(b"bplist"):
        if not raw.startswith(b"bplist00"):
            raise PlistError("unsupported binary property list version (only bplist00)")
        if len(raw) >= 32:  # trailer object count, before plistlib allocates a table
            num_objects = int.from_bytes(raw[-24:-16], "big")
            if num_objects > 40 * config.MAX_DOCUMENT_SEGMENTS:
                raise InputLimitError(
                    "This property list has far more entries than MAX_DOCUMENT_SEGMENTS "
                    f"allows ({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
                )
        try:
            obj = plistlib.loads(raw, fmt=plistlib.FMT_BINARY, dict_type=dict)
        except RecursionError as exc:
            raise InputLimitError("This property list is nested too deeply.") from exc
        except (
            plistlib.InvalidFileException,
            ValueError,
            KeyError,
            IndexError,
            OverflowError,
        ) as exc:
            raise PlistError(f"not a readable binary property list ({exc})") from exc
        try:
            return PlistDoc(root=_tree_from_obj(obj), binary=True)
        except RecursionError as exc:
            raise InputLimitError("This property list is nested too deeply.") from exc
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        bom = raw[:2]
        codec = "utf-16-le" if bom == b"\xff\xfe" else "utf-16-be"
    elif raw.startswith(b"\xef\xbb\xbf"):
        bom, codec = raw[:3], "utf-8"
    else:
        bom, codec = b"", "utf-8"
    try:
        text = raw[len(bom) :].decode(codec)
    except UnicodeDecodeError as exc:
        raise PlistError(
            f"property list is not valid {codec.upper()} text; re-save it as UTF-8 XML"
        ) from exc
    head = text.lstrip()[:200]
    if not head.startswith("<"):
        raise PlistError(
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
        raise PlistError("property list declares an encoding other than UTF-8/UTF-16")
    root = _scan_xml(text)
    # Cross-check with the stdlib reader (expat; it refuses entity declarations). The
    # declaration is dropped so expat reads the already-decoded text consistently.
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)
    try:
        ref = plistlib.loads(
            ('<?xml version="1.0" encoding="UTF-8"?>' + body).encode("utf-8"),
            fmt=plistlib.FMT_XML,
            dict_type=dict,
        )
    except RecursionError as exc:
        raise InputLimitError("This property list is nested too deeply.") from exc
    except Exception as exc:
        raise PlistError(f"not a well-formed property list ({exc})") from exc
    if not _same_shape(root, _tree_from_obj(ref)):
        raise PlistError("property list could not be read unambiguously; refused")
    return PlistDoc(root=root, binary=False, text=text, bom=bom, codec=codec)


def _obj_from_tree(doc: PlistDoc, node: Node):
    if node.kind == "string":
        return doc.edits.get(id(node), node.text)
    if node.kind == "array":
        return [_obj_from_tree(doc, c) for c in node.children]
    if node.kind == "dict":
        extra: dict[int, list] = {}
        for d, before, k, v in doc.inserts:
            if d is node:
                extra.setdefault(before, []).append((k, v))
        out = {}
        for i, (k, c) in enumerate(zip(node.keys, node.children, strict=True)):
            for ek, ev in extra.get(i, []):
                out[ek] = ev
            out[k] = _obj_from_tree(doc, c)
        return out
    return node.obj


def write_plist(doc: PlistDoc, original: bytes) -> bytes:
    if not doc.edits and not doc.inserts:
        return bytes(original)
    if doc.binary:
        obj = _obj_from_tree(doc, doc.root)
        return plistlib.dumps(obj, fmt=plistlib.FMT_BINARY, sort_keys=False)
    text = doc.text
    splices: list[tuple[int, int, str]] = []
    for node, new in _string_nodes_with_edits(doc):
        if node.cstart >= 0:
            splices.append((node.cstart, node.cend, escape_xml(new)))
        else:  # a self-closing <string/> that now has a value
            splices.append((node.estart, node.eend, f"<string>{escape_xml(new)}</string>"))
    for d, before, k, v in doc.inserts:
        knode, vnode = d.key_nodes[before], d.children[before]
        prev_end = d.children[before - 1].eend if before else _dict_content_start(text, d)
        gap = text[prev_end : knode.estart]
        gap = gap if gap.strip() == "" and gap else "\n"
        kv_gap = text[knode.eend : vnode.estart]
        kv_gap = kv_gap if kv_gap.strip() == "" else ""
        piece = f"<key>{escape_xml(k)}</key>{kv_gap}<string>{escape_xml(v)}</string>{gap}"
        splices.append((knode.estart, knode.estart, piece))
    splices.sort(key=lambda s: (s[0], s[1]))
    out: list[str] = []
    pos = 0
    for start, end, new in splices:
        out.append(text[pos:start])
        out.append(new)
        pos = max(pos, end)
    out.append(text[pos:])
    return doc.bom + "".join(out).encode(doc.codec)


def _dict_content_start(text: str, d: Node) -> int:
    return text.index(">", d.estart) + 1


def _string_nodes_with_edits(doc: PlistDoc):
    stack = [doc.root]
    while stack:
        node = stack.pop()
        if node.kind == "string" and id(node) in doc.edits:
            yield node, doc.edits[id(node)]
        stack.extend(node.children)


# --- which strings translate ----------------------------------------------------------

_INFO_KEYS = {
    "CFBundleDisplayName",
    "CFBundleName",
    "CFBundleSpokenName",
    "CFBundleGetInfoString",
    "NSHumanReadableCopyright",
    "CFBundleTypeName",
    "UTTypeDescription",
    "INAlternativeAppName",
    "NSMenuItem",
    "default",  # NSServices NSMenuItem {default = "..."}
}
_SETTINGS_KEYS = {"Title", "Titles", "ShortTitles", "FooterText"}
_STRINGSDICT_SKIP = {"NSStringFormatSpecTypeKey", "NSStringFormatValueTypeKey"}
_NOT_PROSE = re.compile(
    r"^(?:"
    r"[\d\s.,:/+-]+"  # numbers, versions, dates
    r"|\$\([^)]*\)[^\s]*"  # $(PRODUCT_NAME) build settings
    r"|\$\{[^}]*\}[^\s]*"
    r"|[A-Za-z][\w-]*(?:\.[\w-]+)+"  # reverse-DNS ids, file names
    r"|[A-Za-z][A-Za-z0-9+.-]*://\S*"  # URLs
    r"|[A-Za-z_]\w*_\w+"  # snake_case identifiers
    r"|[a-z]+[A-Z]\w*"  # camelCase identifiers
    r"|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*){2,}"  # UIInterfaceOrientationPortrait
    r"|#[0-9A-Fa-f]{3,8}"  # colours
    r")$"
)


def plist_mode(root: Node) -> str:
    if root.kind == "dict":
        if "CFBundleIdentifier" in root.keys or any(k.startswith("CFBundle") for k in root.keys):
            return "info"
        if "PreferenceSpecifiers" in root.keys:
            return "settings"
    return "generic"


def walk_strings(root: Node):
    """Every string leaf in document order with the chain of dict keys above it."""

    def rec(node: Node, keys: tuple, path: tuple):
        if node.kind == "string":
            yield node, keys, path
        elif node.kind == "dict":
            for k, c in zip(node.keys, node.children, strict=True):
                yield from rec(c, (*keys, k), (*path, k))
        elif node.kind == "array":
            for i, c in enumerate(node.children):
                yield from rec(c, keys, (*path, str(i)))

    yield from rec(root, (), ())


def _wanted(fmt: str, mode: str, keys: tuple, value: str) -> bool:
    if not is_translatable(value):
        return False
    near = keys[-1] if keys else ""
    if fmt == "stringsdict":
        return near not in _STRINGSDICT_SKIP
    if mode == "info":
        return (
            near in _INFO_KEYS
            or near.endswith("UsageDescription")
            or any(k.endswith("UsageDescriptionDictionary") for k in keys)
        )
    if mode == "settings":
        return near in _SETTINGS_KEYS
    return not _NOT_PROSE.match(value.strip())


def translatable_leaves(fmt: str, doc: PlistDoc) -> list[tuple[Node, tuple, tuple]]:
    mode = plist_mode(doc.root) if fmt == "plist" else ""
    out = []
    for node, keys, path in walk_strings(doc.root):
        if _wanted(fmt, mode, keys, node.text):
            out.append((node, keys, path))
            if len(out) > config.MAX_DOCUMENT_SEGMENTS:
                check_segment_cap(len(out))
    return out


# --- the filters ----------------------------------------------------------------------


class _PlistFilterBase(Filter):
    fmt = "plist"

    def _read(self, data: bytes) -> PlistDoc:
        if not isinstance(data, (bytes, bytearray)):
            data = str(data).encode("utf-8")
        return read_plist(bytes(data))

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        doc = self._read(data)
        units: list[TextUnit] = []
        for node, _keys, _path in translatable_leaves(self.fmt, doc):
            _lead, text, _trail, _tokens = unit_text(node.text)
            units.append(TextUnit(p_index=len(units), text=text))
        skeleton = {
            "format": self.fmt,
            "plist_encoding": "binary" if doc.binary else "xml",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        if not doc.binary and doc.codec != "utf-8":
            note_source_encoding(skeleton, doc.codec)
        return ParseResult(skeleton=skeleton, units=units)

    def _apply(self, doc: PlistDoc, paragraph_texts: dict) -> None:
        for i, (node, _keys, _path) in enumerate(translatable_leaves(self.fmt, doc)):
            new = apply_translation(node.text, paragraph_texts.get(str(i)))
            if new is not None:
                doc.edits[id(node)] = new

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        doc = self._read(original)
        self._apply(doc, paragraph_texts)
        return write_plist(doc, original)

    def records(self, data: bytes) -> list[KeyValueRecord]:
        """Every string value as a record (key = its path joined with '/')."""
        doc = self._read(data)
        mode = plist_mode(doc.root)
        out: list[KeyValueRecord] = []
        k = 0
        for node, keys, path in walk_strings(doc.root):
            idx = None
            if _wanted(self.fmt, mode, keys, node.text):
                idx = k
                k += 1
            out.append(KeyValueRecord(key="/".join(path), value=node.text, unit_index=idx))
        return out


@register
class PlistFilter(_PlistFilterBase):
    """Info.plist, Settings.bundle Root.plist and generic property lists."""

    fmt = "plist"


def _plural_rule_dicts(root: Node):
    """(entry key, variable name, variable dict node) for every NSStringPluralRuleType."""
    if root.kind != "dict":
        return
    for ekey, entry in zip(root.keys, root.children, strict=True):
        if entry.kind != "dict":
            continue
        for vname, var in zip(entry.keys, entry.children, strict=True):
            if var.kind != "dict" or "NSStringFormatSpecTypeKey" not in var.keys:
                continue
            spec = var.children[var.keys.index("NSStringFormatSpecTypeKey")]
            if spec.kind == "string" and spec.text == "NSStringPluralRuleType":
                yield ekey, vname, var


@register
class StringsdictFilter(_PlistFilterBase):
    """Apple .stringsdict: plural (NSStringPluralRuleType), device-specific and
    variable-width rules. Keys and type tags are never translated."""

    fmt = "stringsdict"

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        doc = self._read(original)
        self._apply(doc, paragraph_texts)
        cats = target_plural_categories(target_locale)
        if cats:
            for _ekey, _vname, var in _plural_rule_dicts(doc.root):
                if "other" not in var.keys:
                    continue
                oi = var.keys.index("other")
                other = var.children[oi]
                if other.kind != "string":
                    continue
                fill = doc.edits.get(id(other), other.text)
                for cat in cats:
                    if cat not in var.keys:
                        doc.inserts.append((var, oi, cat, fill))
        return write_plist(doc, original)

    def records(self, data: bytes) -> list[KeyValueRecord]:
        """One record per entry: the format key as value, its plural forms (first plural
        variable; further variables get their own record with context = variable name),
        and device or width variants flattened into plural_forms-style dicts."""
        doc = self._read(data)
        root = doc.root
        index: dict[int, int] = {}
        for i, (node, _k, _p) in enumerate(translatable_leaves(self.fmt, doc)):
            index[id(node)] = i
        out: list[KeyValueRecord] = []
        if root.kind != "dict":
            return out
        for ekey, entry in zip(root.keys, root.children, strict=True):
            if entry.kind == "string":
                out.append(
                    KeyValueRecord(key=ekey, value=entry.text, unit_index=index.get(id(entry)))
                )
                continue
            if entry.kind != "dict":
                continue
            fmt_value, fmt_idx = "", None
            if "NSStringLocalizedFormatKey" in entry.keys:
                fk = entry.children[entry.keys.index("NSStringLocalizedFormatKey")]
                if fk.kind == "string":
                    fmt_value, fmt_idx = fk.text, index.get(id(fk))
            variants = []
            for vname, var in zip(entry.keys, entry.children, strict=True):
                if vname == "NSStringLocalizedFormatKey" or var.kind != "dict":
                    continue
                forms = {
                    k: c.text
                    for k, c in zip(var.keys, var.children, strict=True)
                    if c.kind == "string" and k not in _STRINGSDICT_SKIP
                }
                first = next((index[id(c)] for c in var.children if id(c) in index), None)
                # keys bridge contract: each plural form names the unit that carries it
                units = {
                    k: index[id(c)]
                    for k, c in zip(var.keys, var.children, strict=True)
                    if k in forms and id(c) in index
                }
                variants.append((vname, forms, first, units))
            if not variants:
                out.append(KeyValueRecord(key=ekey, value=fmt_value, unit_index=fmt_idx))
                continue
            for n, (vname, forms, first, units) in enumerate(variants):
                out.append(
                    KeyValueRecord(
                        key=ekey,
                        value=fmt_value,
                        context="" if n == 0 else vname,
                        plural_forms=forms,
                        plural_units=units,
                        unit_index=fmt_idx if fmt_idx is not None else first,
                    )
                )
        return out


# --- String Catalog (.xcstrings) ---------------------------------------------------
#
# A String Catalog is one JSON file holding EVERY language:
#
#     {"sourceLanguage": "en", "version": "1.0", "strings": {
#         "<key>": {"comment": "...", "extractionState": "manual", "shouldTranslate": false,
#                   "localizations": {"<lang>": {
#                       "stringUnit": {"state": "translated", "value": "..."},
#                       "variations": {"plural": {"one": {...}, "other": {...}},
#                                      "device": {"iphone": {...}, "mac": {...}}},
#                       "substitutions": {"<name>": {"argNum": 1, "formatSpecifier": "lld",
#                                                    "variations": {"plural": {...}}}}}}}}}
#
# Units: the SOURCE-language text of every string, in file order: each stringUnit value
# under the source localization (plural, device and substitution variants each their own
# unit), or the key itself when the catalog has no source localization for it (Xcode's
# default: the key is the English text). `shouldTranslate: false` and
# `extractionState: "stale"` entries are skipped but walked, like json_filter's
# untranslatable strings. Placeholders become ⟦xN⟧ tokens (tokenize above).
#
# build_localized(target) adds or replaces ONLY the target localization of each translated
# string and leaves every other language untouched. The new localization mirrors the source
# shape; plural variations use the TARGET language's CLDR categories (babel), filled from
# the same category of the source or else from `other`; the state is "translated". A
# string whose units were not translated keeps its existing target localization.
# build() (no locale) writes the translations into the source localization in place,
# the same "replace the text" contract as every monolingual filter.
#
# The file is re-serialized in the source's own JSON style (Xcode: two-space indent,
# `"key" : value`, unescaped UTF-8, original key order; a new locale is inserted in
# sorted position when the existing locales are sorted). A build with no change returns
# the original bytes.

_SKIP_STATES = {"stale"}


class XcstringsError(ValueError):
    """Not a String Catalog (caught by the upload route as a polite parse refusal)."""


def _decode(data: bytes) -> tuple[str, bool]:
    raw = bytes(data)
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        return raw.decode("utf-8-sig"), bom
    except UnicodeDecodeError as exc:
        raise XcstringsError(
            "String Catalog is not valid UTF-8 (byte 0x%02x at position %d); "  # noqa: UP031
            "re-save it from Xcode." % (raw[exc.start], exc.start)
        ) from exc


def _load(data: bytes) -> tuple[dict, str, bool]:
    text, bom = _decode(data)
    check_json_budget(text, "This String Catalog")
    try:
        obj = json.loads(text)
    except RecursionError as exc:
        raise InputLimitError("This String Catalog is nested too deeply.") from exc
    except json.JSONDecodeError as exc:
        raise XcstringsError(f"String Catalog is not valid JSON ({exc})") from exc
    if (
        not isinstance(obj, dict)
        or not isinstance(obj.get("strings"), dict)
        or not isinstance(obj.get("sourceLanguage"), str)
    ):
        raise XcstringsError(
            "not a String Catalog: expected an object with sourceLanguage and strings"
        )
    return obj, text, bom


def _loc_leaves(loc, path: tuple, depth: int = 0):
    """(path, value) for every stringUnit under a localization, in file order."""
    if depth > 16:
        raise InputLimitError("This String Catalog is nested too deeply.")
    if not isinstance(loc, dict):
        return
    for key, val in loc.items():
        if key == "stringUnit" and isinstance(val, dict) and isinstance(val.get("value"), str):
            yield (*path, "stringUnit"), val["value"]
        elif key == "variations" and isinstance(val, dict):
            for vtype, vmap in val.items():
                if isinstance(vmap, dict):
                    for cat, sub in vmap.items():
                        yield from _loc_leaves(sub, (*path, "variations", vtype, cat), depth + 1)
        elif key == "substitutions" and isinstance(val, dict):
            for name, sub in val.items():
                yield from _loc_leaves(sub, (*path, "substitutions", name), depth + 1)


def _source_loc(obj: dict, key: str, entry: dict):
    locs = entry.get("localizations")
    src = locs.get(obj["sourceLanguage"]) if isinstance(locs, dict) else None
    if isinstance(src, dict):
        return src, True
    return {"stringUnit": {"state": "translated", "value": key}}, False


def _entry_wanted(entry) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("shouldTranslate") is not False
        and entry.get("extractionState") not in _SKIP_STATES
    )


def _walk(obj: dict):
    """(key, entry, source loc, has explicit source, [(path, value, unit index|None)])."""
    n = 0
    for key, entry in obj["strings"].items():
        if not isinstance(entry, dict):
            continue
        src, explicit = _source_loc(obj, key, entry)
        wanted = _entry_wanted(entry)
        leaves = []
        for path, value in _loc_leaves(src, ()):
            idx = None
            if wanted and is_translatable(value):
                idx = n
                n += 1
                if n > config.MAX_DOCUMENT_SEGMENTS:
                    check_segment_cap(n)
            leaves.append((path, value, idx))
        yield key, entry, src, explicit, leaves


def _get(d, path: tuple):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def _set(d: dict, path: tuple, value) -> None:
    for p in path[:-1]:
        d = d.setdefault(p, {})
    d[path[-1]] = value


def _match_locale(locs: dict, target: str) -> str:
    want = target.replace("_", "-")
    for k in locs:
        if k.replace("_", "-").lower() == want.lower():
            return k
    return want


def _mirror(src: dict, old, trans: dict, path: tuple, cats: list | None) -> dict:
    """The target localization for one string: the source shape, translated leaves (or
    the existing target leaf when that unit has no translation)."""
    old = old if isinstance(old, dict) else {}
    out: dict = {}
    for key, val in src.items():
        if key == "stringUnit" and isinstance(val, dict):
            p = (*path, "stringUnit")
            if p in trans:
                out["stringUnit"] = {"state": "translated", "value": trans[p]}
            elif isinstance(old.get("stringUnit"), dict):
                out["stringUnit"] = old["stringUnit"]
        elif key == "variations" and isinstance(val, dict):
            ovar = old.get("variations") if isinstance(old.get("variations"), dict) else {}
            vout: dict = {}
            for vtype, vmap in val.items():
                if not isinstance(vmap, dict):
                    continue
                order = list(vmap)
                if vtype == "plural" and cats:
                    keep_zero = ["zero"] if "zero" in vmap and "zero" not in cats else []
                    order = [c for c in PLURAL_ORDER if c in set(cats) | set(keep_zero)]
                omap = ovar.get(vtype) if isinstance(ovar.get(vtype), dict) else {}
                cout: dict = {}
                for cat in order:
                    scat = cat if cat in vmap else ("other" if "other" in vmap else None)
                    if scat is None or not isinstance(vmap[scat], dict):
                        continue
                    sub = _mirror(
                        vmap[scat], omap.get(cat), trans, (*path, "variations", vtype, scat), cats
                    )
                    if sub:
                        cout[cat] = sub
                if cout:
                    vout[vtype] = cout
            if vout:
                out["variations"] = vout
        elif key == "substitutions" and isinstance(val, dict):
            osub = old.get("substitutions") if isinstance(old.get("substitutions"), dict) else {}
            sout: dict = {}
            for name, sub in val.items():
                if not isinstance(sub, dict):
                    continue
                m = _mirror(sub, osub.get(name), trans, (*path, "substitutions", name), cats)
                if m:
                    meta = {k: v for k, v in sub.items() if k in ("argNum", "formatSpecifier")}
                    sout[name] = {**meta, **m}
            if sout:
                out["substitutions"] = sout
    return out


def _sorted_deep(d):
    """Xcode writes every object with sorted keys; a new localization follows suit."""
    if isinstance(d, dict):
        return {k: _sorted_deep(d[k]) for k in sorted(d)}
    return d


def _dump(obj: dict, text: str, bom: bool) -> bytes:
    """Serialize in the source's JSON style."""
    ensure_ascii = not any(ord(c) > 127 for c in text) and "\\u" in text
    lines = text.strip().splitlines()
    if len(lines) > 1:
        sep = " : " if '" : ' in text else ": "
        second = lines[1]
        indent = "\t" if second.startswith("\t") else (len(second) - len(second.lstrip(" ")) or 2)
        out = json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, separators=(",", sep))
    else:
        seps = (",", ":") if '":' in text and '": ' not in text else (", ", ": ")
        out = json.dumps(obj, ensure_ascii=ensure_ascii, separators=seps)
    if "\\/" in text:
        out = out.replace("/", "\\/")
    if text.endswith("\n"):
        out += "\n"
    return (b"\xef\xbb\xbf" if bom else b"") + out.encode("utf-8")


@register
class XcstringsFilter(Filter):
    fmt = "xcstrings"
    writes_targets = True  # B-bytes: a target equal to its source is a real write

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        obj, _text, _bom = _load(data)
        units: list[TextUnit] = []
        for _key, _entry, _src, _explicit, leaves in _walk(obj):
            for _path, value, idx in leaves:
                if idx is not None:
                    units.append(TextUnit(p_index=idx, text=unit_text(value)[1]))
        skeleton = {
            "format": "xcstrings",
            "source_language": obj["sourceLanguage"],
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def _translations(self, obj: dict, paragraph_texts: dict):
        """key -> {leaf path: new full value} for the units that carry a translation."""
        out = []
        for key, entry, src, explicit, leaves in _walk(obj):
            trans = {}
            for path, value, idx in leaves:
                if idx is None:
                    continue
                new = paragraph_texts.get(str(idx))
                if new is None:
                    continue
                full = apply_translation(value, new)
                trans[path] = value if full is None else full
            out.append((key, entry, src, explicit, trans))
        return out

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self.build_localized(original, paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        obj, text, bom = _load(original)
        before = copy.deepcopy(obj)
        source = obj["sourceLanguage"]
        in_source = (
            not target_locale
            or target_locale.replace("_", "-").lower() == source.replace("_", "-").lower()
        )
        cats = None if in_source else target_plural_categories(target_locale)
        for key, entry, src, explicit, trans in self._translations(obj, paragraph_texts):
            if not trans:
                continue
            locs = entry.get("localizations")
            if not isinstance(locs, dict):
                locs = {}
            if in_source:
                # build(): replace the source text in place (explicit source only; a
                # key-as-source string gets a source localization holding the new text).
                if explicit:
                    for path, value in trans.items():
                        unit = _get(src, path)
                        if isinstance(unit, dict) and unit.get("value") != value:
                            unit["value"] = value
                else:
                    value = trans[("stringUnit",)]
                    if value != key:
                        locs[source] = {"stringUnit": {"state": "translated", "value": value}}
                        entry["localizations"] = locs
                continue
            lang = _match_locale(locs, target_locale)
            new_loc = _mirror(src, locs.get(lang), trans, (), cats)
            if not new_loc:
                continue
            entry["localizations"] = locs
            was_sorted = list(locs) == sorted(locs)
            locs[lang] = _sorted_deep(new_loc) if was_sorted else new_loc
            if was_sorted and list(locs) != sorted(locs):
                entry["localizations"] = {k: locs[k] for k in sorted(locs)}
        if obj == before:
            return bytes(original)
        return _dump(obj, text, bom)

    def records(self, data: bytes) -> list[KeyValueRecord]:
        """One record per catalog key: the source text, the developer comment, the source
        plural forms (top-level plural variation; device variants when there is no plural)
        and the first unit index (None when the
        string is not translated: shouldTranslate false, stale, or placeholder-only)."""
        obj, _text, _bom = _load(data)
        out: list[KeyValueRecord] = []
        for key, entry, src, _explicit, leaves in _walk(obj):
            value = _get(src, ("stringUnit", "value"))
            variants = _get(src, ("variations", "plural")) or _get(src, ("variations", "device"))
            forms = None
            if isinstance(variants, dict):
                forms = {
                    c: v["stringUnit"]["value"]
                    for c, v in variants.items()
                    if isinstance(_get(v, ("stringUnit", "value")), str)
                }
                if value is None:
                    value = forms.get("other") or next(iter(forms.values()), "")
            comment = entry.get("comment") if isinstance(entry.get("comment"), str) else ""
            first = next((idx for _p, _v, idx in leaves if idx is not None), None)
            # keys bridge contract: each top-level plural form names the unit carrying it
            units = {
                path[2]: idx
                for path, _v, idx in leaves
                if idx is not None and len(path) == 4 and path[:2] == ("variations", "plural")
            }
            out.append(
                KeyValueRecord(
                    key=key,
                    value=value if isinstance(value, str) else "",
                    comment=comment,
                    plural_forms=forms,
                    plural_units=units or None,
                    unit_index=first,
                )
            )
        return out
