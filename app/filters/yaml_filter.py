"""YAML filter: generic YAML (.yaml/.yml), Rails i18n YAML and Symfony translation YAML
(file-format Batch 3).

Design (event-stream splice, no new dependency): PyYAML's SafeLoader parses the file into
EVENTS only (never `yaml.load`, never a constructed tree, so aliases are never expanded and
no Python object is ever built from the input). Every scalar event carries the exact span of
its source text, so parse() lists the translatable value scalars in document order and
build() splices each translated value back into that span, leaving every other character
alone: comments, keys, anchors, tags, merge keys, indentation, quoting style of untouched
values, blank lines, line endings and the trailing newline all survive byte for byte. An
untranslated (or unchanged) value keeps its original spelling.

What is translated: string VALUE scalars (never keys) that resolve to `str` under YAML 1.1
(the resolver Psych, Symfony and PyYAML use): `yes`/`no`/`on`/`off`, numbers, dates and
`null`/`~` are skipped, as are scalars with a non-string explicit tag (`!!int`,
`!ruby/object`, ...) and strings with no letter or digit. Skipped scalars are still walked,
in the same order at parse and build, so unit indices line up (json_filter rule). An alias
(`*a`) is not a scalar event, so an anchored value is translated exactly once.

Re-emitting a translated value: plain stays plain when the new text is still a plain YAML
string in that context (no leading indicator, no `: ` or ` #`, not a bool/number, no flow
punctuation inside `[...]`/`{...}`), otherwise it becomes a double-quoted scalar; single
quoted stays single quoted on one line; double quoted is re-escaped; literal `|` and folded
`>` blocks keep their header (chomping indicator and any comment) and indentation (a folded
block whose translation has line breaks becomes literal so the breaks survive). Leading and
trailing whitespace of the source value is kept outside the unit text and restored.

Rails i18n: a file whose single root key is a locale code (`en:`, `pt-BR:`) holding a
mapping is a Rails locale file; build_localized() renames that root key to the target
locale and gives each plural map the target's missing CLDR categories, filled with the
translated `other` (#729, the shared rule in app/filters/plurals.py). Symfony translation
files carry the locale in the FILE NAME (`messages.fr.yaml`), never in the content, so they
are generic YAML here.

records(): the key-based view Batch 9 wires into `keys import/export`: one record per value
scalar with its dotted key path (Rails root stripped), source comment lines directly above
the key, and CLDR plural maps (`one:`/`other:` ...) folded into one record.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from app import config
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    check_unit_texts,
    decode_text_lossless,
    encode_text_lossless,
    note_source_encoding,
    read_text_upload,
    register,
)

try:  # Batch 1 owns the shared record type; until it lands this mirror keeps the shape.
    from app.filters.kv_resource import KeyValueRecord  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - replaced when kv_resource merges

    @dataclass
    class KeyValueRecord:  # type: ignore[no-redef]
        key: str
        value: str
        comment: str = ""
        context: str = ""
        plural_forms: dict | None = None
        max_length: int | None = None
        unit_index: int | None = None
        plural_units: dict | None = None
        duplicate: bool = False


_STR_TAG = "tag:yaml.org,2002:str"
_UTF8_BOM = b"\xef\xbb\xbf"
_PLURAL_CATEGORIES = frozenset({"zero", "one", "two", "few", "many", "other"})
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")
_RAILS_3_LETTER = frozenset({"ast", "fil", "gsw", "haw", "ckb", "dsb", "hsb", "nds", "sah", "yue"})
_MAX_DEPTH = 200
# Characters YAML treats as line breaks (1.1) or that must be escaped: only a double-quoted
# scalar can carry them safely.
_NON_PRINTABLE = re.compile(  # PyYAML's own Reader.NON_PRINTABLE
    "[^\x09\x0a\x0d\x20-\x7e\x85\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)
_BREAKS_AND_BOM = re.compile("[\x85\u2028\u2029\ufeff\r]")
_PLAIN_FIRST_BAD = set("-?:,[]{}#&*!|>'\"%@`")
_FLOW_CHARS = set(",[]{}")


def _is_translatable(s: str) -> bool:
    return any(c.isalnum() for c in s)


def check_yaml_budget(text: str, what: str = "This YAML file") -> None:
    """Pre-parse budget (#571 pattern): refuse a file that could hold far more nodes than
    MAX_DOCUMENT_SEGMENTS allows BEFORE PyYAML builds an event for each. Block YAML needs a
    line per node (the shared line budget); flow YAML (`[a,b,{c: d}]`) needs a comma or
    bracket per node, counted at C speed like json_filter.check_json_budget."""
    from app.filters.base import check_line_budget

    check_line_budget(text)
    if text.count(",") + text.count("[") + text.count("{") > 20 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"{what} has far more values than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


def _yaml():
    """PyYAML, imported on first use so `import app.main` and the CLI stay lean."""
    import yaml

    return yaml


@lru_cache(maxsize=1)
def _python_loader():
    yaml = _yaml()

    class _Loader(yaml.SafeLoader):
        """SafeLoader that remembers where each scalar TOKEN starts. A scalar event's own
        start mark points at its anchor or tag when it has one (`&a hello`); the token
        start is the value itself, which is what build() replaces."""

        def __init__(self, stream):
            super().__init__(stream)
            self.scalar_starts: dict[int, int] = {}

        def get_token(self):
            tok = super().get_token()
            if isinstance(tok, yaml.ScalarToken):
                self.scalar_starts[tok.end_mark.index] = tok.start_mark.index
            return tok

    return _Loader


@lru_cache(maxsize=1)
def _resolver():
    """The implicit (YAML 1.1) resolver SafeLoader uses: yes/no/on/off, numbers, dates."""
    return _yaml().resolver.Resolver()


def _resolves_str(value: str, implicit) -> bool:
    return _resolver().resolve(_yaml().ScalarNode, value, implicit) == _STR_TAG


@dataclass
class _Scalar:
    start: int
    end: int
    value: str
    style: str | None
    path: tuple
    translatable: bool
    flow: bool
    key_line: int | None = None


@dataclass
class _Frame:
    kind: str  # "map" | "seq"
    path: tuple
    in_key: bool
    flow: bool
    expecting_key: bool = True
    cur_key: object = None
    idx: int = 0
    key_line: int | None = None
    keys: list = field(default_factory=list)  # (key, scalar_index | None) per entry
    is_key: bool = False  # this collection is itself a (complex) mapping key


@dataclass
class _Walk:
    scalars: list
    plural_maps: list  # (path, {category: scalar_index})
    rails_root: dict | None
    lines: list


def _is_str(ev) -> bool:
    tag = ev.tag
    if tag is None or tag == "!":
        return _resolves_str(ev.value, ev.implicit)
    return tag == _STR_TAG


def _events(text: str):
    """(events iterator, {scalar token end -> token start}). libyaml (C, about 20x faster)
    when PyYAML was built with it: one scan pass for the token starts, one parse pass for
    the events; both index the same characters. Otherwise the pure-Python SafeLoader
    records token starts while it parses."""
    yaml = _yaml()
    if getattr(yaml, "__with_libyaml__", False):
        starts = {
            t.end_mark.index: t.start_mark.index
            for t in yaml.scan(text, Loader=yaml.CSafeLoader)
            if isinstance(t, yaml.ScalarToken)
        }
        return yaml.parse(text, Loader=yaml.CSafeLoader), starts
    loader = _python_loader()(text)

    def gen():
        try:
            while loader.check_event():
                yield loader.get_event()
        finally:
            loader.dispose()

    return gen(), loader.scalar_starts


_STD_COLLECTION_TAGS = {
    None,
    "!",
    "tag:yaml.org,2002:map",
    "tag:yaml.org,2002:seq",
    "tag:yaml.org,2002:omap",
    "tag:yaml.org,2002:pairs",
    "tag:yaml.org,2002:set",
}


def _custom_tag(tag) -> bool:
    return tag not in _STD_COLLECTION_TAGS


def _looks_locale(key: str) -> bool:
    if not _LOCALE_RE.match(key or "") or key.lower() in ("root", "und"):
        return False
    # Gate: babel knows thousands of 3-letter ISO 639-3 codes that are also ordinary root
    # keys (`nav:`, `spa:`), and a generic file misread as Rails gets that key renamed on
    # export. A bare 3-letter key counts only when Rails-i18n actually ships it.
    primary = re.split(r"[-_]", key, maxsplit=1)[0]
    if len(primary) == 3 and primary.lower() not in _RAILS_3_LETTER:
        return False
    try:
        from babel import Locale

        Locale.parse(key, sep="-" if "-" in key else "_")
    except Exception:
        return False
    return True


def _walk(text: str) -> _Walk:
    """The ONE walk parse(), build() and records() share (same units, same order)."""
    yaml = _yaml()
    scalars: list[_Scalar] = []
    plural_maps: list = []
    stack: list[_Frame] = []
    docs = 0
    root_keys: list = []  # (scalar info dict, value kind) for the first document's root map
    root_is_map = False
    count = 0

    def begin():
        """(path, in_key, is_key, flow) for the node that starts now."""
        if not stack:
            return (), False, False, False
        top = stack[-1]
        if top.kind == "map":
            if top.expecting_key:
                return top.path, True, True, top.flow
            return (*top.path, top.cur_key), top.in_key, False, top.flow
        return (*top.path, top.idx), top.in_key, False, top.flow

    def finish(key_value=None, value_kind=None, scalar_index=None):
        if not stack:
            return
        top = stack[-1]
        if top.kind == "map":
            if top.expecting_key:
                top.cur_key = key_value if key_value is not None else "?"
                top.expecting_key = False
            else:
                top.keys.append((top.cur_key, scalar_index))
                if docs == 1 and len(stack) == 1 and root_keys:
                    root_keys[-1][1] = value_kind
                top.expecting_key = True
        else:
            top.idx += 1

    try:
        events, scalar_starts = _events(text)
        for ev in events:
            if isinstance(ev, yaml.DocumentStartEvent):
                docs += 1
            elif isinstance(ev, yaml.ScalarEvent):
                path, in_key, is_key, flow = begin()
                if is_key:
                    top = stack[-1]
                    top.key_line = ev.end_mark.line
                    if docs == 1 and len(stack) == 1:
                        start = scalar_starts.get(ev.end_mark.index)
                        root_keys.append(
                            [
                                {
                                    "key": ev.value,
                                    "start": start,
                                    "end": ev.end_mark.index,
                                    "style": ev.style or None,  # libyaml reports plain as ""
                                },
                                None,
                            ]
                        )
                    finish(key_value=ev.value)
                    continue
                start = scalar_starts.get(ev.end_mark.index)
                ok = start is not None and not in_key and _is_str(ev) and _is_translatable(ev.value)
                key_line = stack[-1].key_line if stack and stack[-1].kind == "map" else None
                scalars.append(
                    _Scalar(
                        start=start if start is not None else ev.end_mark.index,
                        end=ev.end_mark.index,
                        value=ev.value,
                        style=ev.style or None,  # libyaml reports plain as ""
                        path=path,
                        translatable=ok,
                        flow=flow,
                        key_line=key_line,
                    )
                )
                if ok:
                    count += 1
                    check_segment_cap(count)
                finish(value_kind="scalar", scalar_index=len(scalars) - 1)
            elif isinstance(ev, yaml.AliasEvent):
                _path, _in_key, is_key, _flow = begin()
                finish(key_value=("*" + ev.anchor) if is_key else None, value_kind="alias")
            elif isinstance(ev, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                path, in_key, is_key, flow = begin()
                if len(stack) >= _MAX_DEPTH:
                    raise InputLimitError("This YAML file is nested too deeply.")
                kind = "map" if isinstance(ev, yaml.MappingStartEvent) else "seq"
                if not stack and docs == 1 and kind == "map":
                    root_is_map = True
                stack.append(
                    _Frame(
                        kind=kind,
                        path=path,
                        # inside a complex key, or a typed object (`!ruby/object:Foo`):
                        # nothing below is translatable
                        in_key=in_key or is_key or _custom_tag(ev.tag),
                        flow=flow or bool(ev.flow_style),
                        is_key=is_key,
                    )
                )
            elif isinstance(ev, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                frame = stack.pop()
                if frame.kind == "map" and frame.keys and not frame.in_key:
                    cats = {k: i for k, i in frame.keys if isinstance(k, str)}
                    if (
                        len(cats) == len(frame.keys)
                        and "other" in cats
                        and set(cats) <= _PLURAL_CATEGORIES
                        and all(i is not None for i in cats.values())
                    ):
                        plural_maps.append((frame.path, cats))
                if frame.is_key:
                    finish(key_value="?")
                else:
                    finish(value_kind=frame.kind)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        problem = getattr(exc, "problem", None) or getattr(exc, "reason", None) or "invalid YAML"
        raise ValueError(f"This YAML file could not be read: {problem}{where}.") from exc

    rails_root = None
    if docs == 1 and root_is_map and len(root_keys) == 1 and root_keys[0][1] == "map":
        info = root_keys[0][0]
        if (
            info["start"] is not None
            and isinstance(info["key"], str)
            and _looks_locale(info["key"])
        ):
            rails_root = info
    return _Walk(scalars, plural_maps, rails_root, text.split("\n"))


# --------------------------------------------------------------------------- rendering


def _dq(s: str) -> str:
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\x85":
            out.append("\\N")
        elif ch == "\u2028":
            out.append("\\L")
        elif ch == "\u2029":
            out.append("\\P")
        elif ch == "\ufeff" or _NON_PRINTABLE.match(ch):
            out.append(
                f"\\x{o:02X}" if o <= 0xFF else f"\\u{o:04X}" if o <= 0xFFFF else f"\\U{o:08X}"
            )
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _unsafe_unquoted(s: str) -> bool:
    return bool(_NON_PRINTABLE.search(s) or _BREAKS_AND_BOM.search(s))


def _plain_ok(s: str, flow: bool) -> bool:
    if not s or s != s.strip() or "\n" in s or "\t" in s or _unsafe_unquoted(s):
        return False
    if s[0] in _PLAIN_FIRST_BAD or s.startswith("..."):
        return False
    if ": " in s or " #" in s or s.endswith(":"):
        return False
    if flow and any(c in _FLOW_CHARS for c in s):
        return False
    return _resolves_str(s, (True, False))


def _render_block(new: str, sc: _Scalar, region: str) -> str | None:
    """Re-emit a literal/folded block with the source header, indentation and trailing
    blank lines. Returns None when a block cannot carry `new` (caller double-quotes)."""
    nl_at = region.find("\n")
    if nl_at < 0:
        return None
    header = region[: nl_at + 1]
    head = header.rstrip("\r\n")
    if any(c.isdigit() for c in head.split("#", 1)[0]):
        return None  # explicit indentation indicator: keep it simple, double-quote
    rest = region[nl_at + 1 :]
    pieces = re.findall(r"[^\n]*\n|[^\n]+$", rest)
    last = max((i for i, p in enumerate(pieces) if p.strip()), default=None)
    if last is None:
        return None
    content_part = "".join(pieces[: last + 1])
    tail = "".join(pieces[last + 1 :])
    first = pieces[next(i for i, p in enumerate(pieces) if p.strip())]
    indent = first[: len(first) - len(first.lstrip(" "))]
    if not indent:
        return None
    eol = "\r\n" if header.endswith("\r\n") else "\n"
    body = new.rstrip("\n")
    if _unsafe_unquoted(body):
        return None
    lines = body.split("\n")
    first_text = next((ln for ln in lines if ln.strip()), "")
    if not first_text or first_text[0] == " ":
        return None
    if "\n" in body and sc.style == ">":
        header = header.replace(">", "|", 1)
    elif "\n" not in body and sc.style == ">" and body != body.strip():
        return None
    out = "".join((indent + ln if ln else "") + eol for ln in lines)
    if not content_part.endswith("\n"):
        out = out[: -len(eol)]
    return header + out + tail


def _render(new: str, sc: _Scalar, text: str) -> str:
    region = text[sc.start : sc.end]
    if sc.style in ("|", ">"):
        block = _render_block(new, sc, region)
        if block is not None:
            return block
        # A block region ends after its line break(s); a quoted replacement keeps them.
        nl_at = region.find("\n")
        pieces = re.findall(r"[^\n]*\n|[^\n]+$", region[nl_at + 1 :]) if nl_at >= 0 else []
        last = max((i for i, p in enumerate(pieces) if p.strip()), default=-1)
        tail = "".join(pieces[last + 1 :])
        eol = "\r\n" if "\r\n" in region else "\n"
        return _dq(new) + (eol if region.endswith("\n") else "") + tail
    if sc.style == "'" and "\n" not in new and not _unsafe_unquoted(new):
        return "'" + new.replace("'", "''") + "'"
    if sc.style is None and _plain_ok(new, sc.flow):
        return new
    return _dq(new)


def _split_ws(s: str) -> tuple[str, str, str]:
    core = s.strip()
    if not core:
        return s, "", ""
    return s[: len(s) - len(s.lstrip())], core, s[len(s.rstrip()) :]


def _render_key(locale: str, info: dict) -> str:
    if info["style"] == "'":
        return "'" + locale.replace("'", "''") + "'"
    if info["style"] == '"':
        return _dq(locale)
    return locale if _plain_ok(locale, False) else _dq(locale)


def _target_root_key(source_key: str, target_locale: str) -> str:
    tgt = (target_locale or "").strip()
    return tgt.replace("-", "_") if "_" in source_key else tgt.replace("_", "-")


def _plural_fill_edits(walk: _Walk, text: str, values: dict, target_locale: str) -> list:
    """#729 (decision 13): in a Rails locale file, each block plural map (`one:` /
    `other:`) gets the CLDR categories the TARGET uses that it lacks (Polish few and many,
    Arabic zero, two, few and many), each a new line before `other:` holding the translated
    `other` value, so Rails shows the translation for every count. The rule is the one
    every plural-aware filter shares (app/filters/plurals.py). A map whose `other` is not
    translated, or written in flow style (`{one: .., other: ..}`), is left as it is."""
    from app.filters.plurals import missing_categories

    edits: list = []
    line_starts = [0]
    for m in re.finditer("\n", text):
        line_starts.append(m.end())
    for _path, cats in walk.plural_maps:
        idx = cats.get("other")
        value = values.get(idx) if idx is not None else None
        sc = walk.scalars[idx] if idx is not None else None
        if value is None or sc is None or sc.flow or sc.key_line is None:
            continue
        missing = missing_categories(cats, target_locale)
        if not missing or sc.key_line >= len(line_starts):
            continue
        bol = line_starts[sc.key_line]
        line = text[bol : text.find("\n", bol) if "\n" in text[bol:] else len(text)]
        indent = line[: len(line) - len(line.lstrip(" "))]
        if not re.match(r"""(other|'other'|"other")\s*:""", line[len(indent) :]):
            continue
        rendered = value if _plain_ok(value, False) else _dq(value)
        eol = "\r\n" if line.endswith("\r") else "\n"
        edits.append((bol, bol, "".join(f"{indent}{c}: {rendered}{eol}" for c in missing)))
    return edits


@register
class YamlFilter(Filter):
    fmt = "yaml"
    exts = ("yml",)

    @staticmethod
    def _read(data: bytes, *, build: bool = False) -> tuple[str, str]:
        if build:
            return decode_text_lossless(data, sig=True), "utf-8"
        return read_text_upload(data, sig=True)

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text, enc = self._read(data)
        check_yaml_budget(text)
        walk = _walk(text)
        units: list[TextUnit] = []
        for sc in walk.scalars:
            if sc.translatable:
                _lead, core, _trail = _split_ws(sc.value)
                units.append(TextUnit(p_index=len(units), text=core))
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "yaml",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        if walk.rails_root is not None:
            skeleton["rails_locale"] = walk.rails_root["key"]
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def _build_text(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None
    ) -> bytes:
        raw = bytes(original)
        text, _enc = self._read(raw, build=True)
        check_yaml_budget(text)
        walk = _walk(text)
        edits: list[tuple[int, int, str]] = []
        i = 0
        values: dict[int, str] = {}  # scalar index -> translated value
        for n, sc in enumerate(walk.scalars):
            if not sc.translatable:
                continue
            new_core = paragraph_texts.get(str(i))
            i += 1
            if new_core is None:
                continue
            lead, _core, trail = _split_ws(sc.value)
            new_value = lead + str(new_core) + trail
            values[n] = new_value
            if new_value == sc.value:
                continue
            edits.append((sc.start, sc.end, _render(new_value, sc, text)))
        root = walk.rails_root
        if target_locale and root is not None:
            new_key = _target_root_key(root["key"], target_locale)
            if new_key and new_key != root["key"]:
                edits.append((root["start"], root["end"], _render_key(new_key, root)))
            edits.extend(_plural_fill_edits(walk, text, values, target_locale))
        parts: list[str] = []
        pos = 0
        for start, end, rep in sorted(edits):
            parts.append(text[pos:start])
            parts.append(rep)
            pos = end
        parts.append(text[pos:])
        out = "".join(parts)
        bom = _UTF8_BOM if raw.startswith(_UTF8_BOM) else b""
        return bom + encode_text_lossless(out)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._build_text(original, paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() plus, for a Rails locale file, the root locale key renamed to the
        target (`en:` -> `de:`). Generic and Symfony YAML carry no in-file language."""
        return self._build_text(original, paragraph_texts, target_locale)

    def records(self, data: bytes) -> list[KeyValueRecord]:
        """Key-based view (Batch 9): one record per value scalar, translatable or not.
        Keys are dotted paths (`errors.messages.blank`, list items `[0]`), the Rails
        locale root stripped. A CLDR plural map (`one:`/`other:` ...) is ONE record with
        `plural_forms`; its `value` is the `other` form."""
        text, _enc = self._read(data)
        check_yaml_budget(text)
        walk = _walk(text)
        strip_root = 1 if walk.rails_root is not None else 0
        unit_of: dict[int, int] = {}
        n = 0
        for idx, sc in enumerate(walk.scalars):
            if sc.translatable:
                unit_of[idx] = n
                n += 1
        in_plural: dict[int, tuple] = {}
        for path, cats in walk.plural_maps:
            for idx in cats.values():
                in_plural[idx] = path

        def dotted(path: tuple) -> str:
            out = ""
            for seg in path[strip_root:]:
                if isinstance(seg, int):
                    out += f"[{seg}]"
                else:
                    out += ("." if out else "") + str(seg)
            return out

        def comment_above(line: int | None) -> str:
            if line is None:
                return ""
            got = []
            j = line - 1
            while j >= 0 and walk.lines[j].strip().startswith("#"):
                got.append(walk.lines[j].strip().lstrip("#").strip())
                j -= 1
            return "\n".join(reversed(got))

        records: list[KeyValueRecord] = []
        seen: set = set()
        emitted_plural: set = set()
        for idx, sc in enumerate(walk.scalars):
            if idx in in_plural:
                path = in_plural[idx]
                if path in emitted_plural:
                    continue
                emitted_plural.add(path)
                cats = next(c for p, c in walk.plural_maps if p == path)
                forms = {cat: walk.scalars[i].value for cat, i in cats.items()}
                key = dotted(path)
                records.append(
                    KeyValueRecord(
                        key=key,
                        value=forms["other"],
                        comment=comment_above(walk.scalars[cats["other"]].key_line),
                        plural_forms=forms,
                        unit_index=unit_of.get(cats["other"]),
                        plural_units={cat: unit_of.get(i) for cat, i in cats.items()},
                        duplicate=key in seen,
                    )
                )
                seen.add(key)
                continue
            key = dotted(sc.path)
            records.append(
                KeyValueRecord(
                    key=key,
                    value=sc.value,
                    comment=comment_above(sc.key_line),
                    unit_index=unit_of.get(idx),
                    duplicate=key in seen,
                )
            )
            seen.add(key)
        return records
