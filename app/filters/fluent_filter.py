"""Project Fluent (.ftl) filter.

Syntax: Fluent 1.0, https://projectfluent.org/fluent/guide/ and the EBNF at
https://github.com/projectfluent/fluent/blob/master/spec/fluent.ebnf . Parsed with
fluent.syntax (Apache-2.0, https://github.com/projectfluent/python-fluent), with
spans on, so build() SPLICES translations into the exact source spans and every
other byte (comments, identifiers, indentation, Junk, line endings, BOM) is kept.

Units (parse and build walk the same tree in the same order):
  * a message value and each message attribute (`.title`, `.aria-label`,
    `.accesskey`) are translatable patterns;
  * a pattern is cut into RUNS: maximal stretches of text and simple placeables
    (`{ $var }`, `{ -term }`, `{ msg.attr }`, `{ NUMBER($n) }`, string literals);
  * a select expression (`{ $n -> [one] ... *[other] ... }`) ends a run, and each
    VARIANT's pattern is walked on its own, so `[one]` and `[other]` are separate,
    plural-aware units (style `ftl:id[one]`), exactly like PO msgid/msgid_plural;
    the selector and the variant keys are never touched. Fluent falls back to the
    `*` default variant for any plural category the target language has that the
    source file lacks, so the file stays valid for every locale; build_localized()
    also writes those categories out, each a copy of the translated `[other]`
    variant (#729, the shared plural-fill rule in app/filters/plurals.py).
  * terms (`-brand-name = Firefox`) are brand and product names: NEVER translated
    unless the operator sets FTL_TRANSLATE_TERMS=1 (environment). Term attributes
    (`.gender`) are grammatical metadata and are never translated.

Placeables stay in the unit text as written (`{ $name }`, `{ -brand-name }`): the
shared segment tag patterns protect `{...}` so MT keeps them verbatim. On build every
original placeable found in the translation is written back as it was; any other
`{`/`}` the translation carries is escaped as a string literal (`{"{"}`), and a
continuation line that would start with `[`, `*` or `.` is escaped the same way,
so a translation can never change the structure of the file.

Security: UTF-8 only (the spec's encoding); a brace/line budget and a nesting cap run
before the parser (the parser recurses on nested placeables); nothing is evaluated.
"""

from __future__ import annotations

import os
import re

from app import config
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    UnsupportedEncodingError,
    _looks_binary,
    check_line_budget,
    check_segment_cap,
    register,
)

_MAX_NESTING = 40  # placeables inside placeables; real files use 2-3
_BOM = b"\xef\xbb\xbf"
_ESC_BRACE = {"{": '{"{"}', "}": '{"}"}'}
_LINE_START_SPECIAL = "[*."


def _translate_terms() -> bool:
    return os.environ.get("FTL_TRANSLATE_TERMS", "").strip().lower() in ("1", "true", "yes", "on")


def _decode(data: bytes) -> tuple[str, bytes]:
    data = bytes(data)
    bom = _BOM if data.startswith(_BOM) else b""
    body = data[len(bom) :]
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        if _looks_binary(body):
            raise UnsupportedEncodingError(
                "file is not valid UTF-8 and looks like a binary file, not text; "
                "re-save the file as UTF-8."
            ) from exc
        raise UnsupportedEncodingError(
            f"Fluent .ftl files are UTF-8; byte 0x{body[exc.start]:02x} at position "
            f"{exc.start + len(bom)} is not. Re-save the file as UTF-8."
        ) from exc
    if "\x00" in text:
        raise UnsupportedEncodingError(
            "file is not valid UTF-8 text (it holds NUL bytes); re-save the file as UTF-8."
        )
    return text, bom


def check_ftl_budget(text: str) -> None:
    """Pre-parse budget: line count, placeable count and nesting depth, all at C speed
    or in one linear pass, before fluent.syntax builds a tree."""
    check_line_budget(text)
    if text.count("{") > 20 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"This Fluent file has far more placeables than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )
    depth = 0
    for ch in re.findall(r"[{}]", text):
        depth = depth + 1 if ch == "{" else max(0, depth - 1)
        if depth > _MAX_NESTING:
            raise InputLimitError(
                f"This Fluent file nests placeables more than {_MAX_NESTING} deep."
            )


class _Run:
    """One translatable stretch of a pattern."""

    __slots__ = ("end", "indent", "label", "placeables", "start", "text")

    def __init__(self, start, end, text, placeables, label, indent):
        self.start = start
        self.end = end
        self.text = text  # what the translator sees (dedented, placeables as written)
        self.placeables = placeables  # placeable source strings, longest first
        self.label = label
        self.indent = indent


def _is_select(el, ast) -> bool:
    return isinstance(el, ast.Placeable) and isinstance(el.expression, ast.SelectExpression)


def _brace_literal(el, ast) -> str | None:
    """`{ "{" }`, `{ "}" }` and the line-start escapes `{ "[" }`, `{ "*" }`, `{ "." }`:
    shown to the translator as the plain character (build escapes it again)."""
    if (
        isinstance(el, ast.Placeable)
        and isinstance(el.expression, ast.StringLiteral)
        and el.expression.value in ("{", "}", "[", "*", ".")
    ):
        return el.expression.value
    return None


def _indent_of(src: str, start: int, end: int) -> str:
    """Indentation for continuation lines: the run's own, else its line's plus four."""
    m = re.search(r"\n([ ]+)\S", src[start:end])
    if m:
        return m.group(1)
    bol = max(src.rfind("\n", 0, start), src.rfind("\r", 0, start)) + 1
    line = src[bol:start]
    return line[: len(line) - len(line.lstrip(" "))] + "    "


def _walk_pattern(pattern, label, src, ast, out: list) -> None:
    run: list = []

    def flush():
        if not run:
            return
        start, end = run[0].span.start, run[-1].span.end
        parts: list[str] = []
        placeables: set[str] = set()
        for el in run:
            if isinstance(el, ast.TextElement):
                parts.append(el.value)
                continue
            lit = _brace_literal(el, ast)
            if lit is not None:
                parts.append(lit)
                continue
            raw = src[el.span.start : el.span.end]
            parts.append(raw)
            placeables.add(raw)
        out.append(
            _Run(
                start,
                end,
                "".join(parts),
                sorted(placeables, key=len, reverse=True),
                label,
                _indent_of(src, start, end),
            )
        )
        run.clear()

    for el in pattern.elements:
        if _is_select(el, ast):
            flush()
            for variant in el.expression.variants:
                key = variant.key
                name = key.name if isinstance(key, ast.Identifier) else str(key.value)
                _walk_pattern(variant.value, f"{label}[{name}]", src, ast, out)
            continue
        run.append(el)
    flush()


def _runs(text: str) -> list[_Run]:
    from fluent.syntax import FluentParser
    from fluent.syntax import ast as fast

    try:
        resource = FluentParser(with_spans=True).parse(text)
    except RecursionError as exc:
        raise InputLimitError("This Fluent file nests placeables too deeply.") from exc
    out: list[_Run] = []
    terms = _translate_terms()
    for entry in resource.body:
        if isinstance(entry, fast.Message):
            ident = entry.id.name
            if entry.value is not None:
                _walk_pattern(entry.value, ident, text, fast, out)
            for attr in entry.attributes:
                _walk_pattern(attr.value, f"{ident}.{attr.id.name}", text, fast, out)
        elif isinstance(entry, fast.Term) and terms:
            # Term attributes are grammatical metadata (gender, case): never translated.
            _walk_pattern(entry.value, f"-{entry.id.name}", text, fast, out)
        # Comments, GroupComments, ResourceComments and Junk carry no units.
    return out


def _translatable(s: str, placeables: list[str]) -> bool:
    for p in placeables:
        s = s.replace(p, "")
    return any(c.isalnum() for c in s)


def _split_ws(s: str) -> tuple[str, str, str]:
    core = s.strip()
    if not core:
        return s, "", ""
    lead = s[: len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()) :]
    return lead, core, trail


def _escape_text(seg: str, at_line_start: bool) -> str:
    out: list[str] = []
    line_start = at_line_start
    for ch in seg:
        if ch in _ESC_BRACE:
            out.append(_ESC_BRACE[ch])
            line_start = False
            continue
        if line_start and ch in _LINE_START_SPECIAL:
            out.append('{"' + ch + '"}')
            line_start = False
            continue
        out.append(ch)
        if ch == "\n":
            line_start = True
        elif ch != " ":
            line_start = False
    return "".join(out)


def _encode(new: str, run: _Run, newline: str) -> str:
    """Translator text -> FTL source for the run's span."""
    new = new.replace("\r\n", "\n").replace("\r", "\n")
    pieces: list[str] = []
    i, n = 0, len(new)
    pat = re.compile("|".join(map(re.escape, run.placeables))) if run.placeables else None
    while i < n:
        m = pat.search(new, i) if pat else None
        j = m.start() if m else n
        if j > i:
            before = pieces[-1] if pieces else ""
            at_start = bool(before.endswith("\n"))
            pieces.append(_escape_text(new[i:j], at_start))
        if m:
            pieces.append(m.group(0))
            i = m.end()
        else:
            i = n
    body = "".join(pieces)
    # Continuation lines carry the pattern's indentation; a blank line keeps none.
    lines = body.split("\n")
    rebuilt = lines[0]
    for line in lines[1:]:
        rebuilt += newline + (run.indent + line if line.strip() else "")
    return rebuilt


def _plural_selects(text: str):
    """Every select expression of the file's messages (and terms), nested ones included."""
    from fluent.syntax import FluentParser
    from fluent.syntax import ast as fast

    found: list = []

    def walk(pattern):
        for el in pattern.elements if pattern is not None else ():
            if _is_select(el, fast):
                found.append(el.expression)
                for variant in el.expression.variants:
                    walk(variant.value)

    try:
        resource = FluentParser(with_spans=True).parse(text)
    except RecursionError:
        return []
    for entry in resource.body:
        if isinstance(entry, (fast.Message, fast.Term)):
            walk(entry.value)
            for attr in entry.attributes:
                walk(attr.value)
    return found


def _is_ordinal(selector, fast) -> bool:
    """`NUMBER($n, type: "ordinal")`: the select keys are ordinal categories (gate, #729)."""
    if not isinstance(selector, fast.FunctionReference) or selector.id.name != "NUMBER":
        return False
    for arg in selector.arguments.named:
        if arg.name.name == "type" and getattr(arg.value, "value", None) == "ordinal":
            return True
    return False


def _line_start(text: str, pos: int) -> int:
    return max(text.rfind("\n", 0, pos), text.rfind("\r", 0, pos)) + 1


def _variant_fill_edits(text: str, edits: list, locale: str, splice) -> list:
    """Insertions (pos, pos, text) adding the target's missing plural variants (see
    FluentFilter.build_localized); `edits` are the translation splices, so a new variant
    copies the TRANSLATED `[other]` body."""
    from fluent.syntax import ast as fast

    from app.filters.plurals import PLURAL_ORDER, missing_categories

    out: list = []
    for sel in _plural_selects(text):
        named = {v.key.name: v for v in sel.variants if isinstance(v.key, fast.Identifier)}
        if len(named) < 2 or any(n not in PLURAL_ORDER for n in named):
            continue  # not a plural select (`[male]`), or nothing to copy from
        other = named.get("other")
        default = next((v for v in sel.variants if v.default), None)
        missing = missing_categories(named, locale, ordinal=_is_ordinal(sel.selector, fast))
        if other is None or default is None or not missing:
            continue
        close = text.find("]", other.key.span.end)
        if close < 0 or close >= other.span.end:
            continue
        body = splice(text, edits, close + 1, other.span.end)
        bol = _line_start(text, default.span.start)
        indent = text[bol : default.span.start]
        if indent.strip():
            continue  # the default variant does not start its own line
        first = sel.variants[0]
        lead = text[_line_start(text, first.span.start) : first.span.start]
        lead = lead if not first.default and not lead.strip() else indent + " "
        newline = "\r\n" if "\r\n" in text else "\n"
        out.append((bol, bol, "".join(f"{lead}[{c}]{body}{newline}" for c in missing)))
    return out


@register
class FluentFilter(Filter):
    fmt = "ftl"

    def _read(self, data: bytes) -> tuple[str, bytes, list[_Run]]:
        text, bom = _decode(data)
        check_ftl_budget(text)
        runs = _runs(text)
        return text, bom, runs

    @staticmethod
    def _units(runs: list[_Run]):
        k = 0
        for run in runs:
            if not _translatable(run.text, run.placeables):
                continue
            yield k, run
            k += 1

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        _text, _bom, runs = self._read(data)
        units: list[TextUnit] = []
        for idx, run in self._units(runs):
            _lead, core, _trail = _split_ws(run.text)
            if len(core) > config.MAX_SEGMENT_CHARS:
                raise InputLimitError(
                    f"This file has a message longer than {config.MAX_SEGMENT_CHARS} "
                    "characters (MAX_SEGMENT_CHARS). Break it into shorter messages."
                )
            units.append(TextUnit(p_index=idx, text=core, style=f"ftl:{run.label}"))
            if len(units) > config.MAX_DOCUMENT_SEGMENTS:
                check_segment_cap(len(units))
        skeleton = {
            "format": self.fmt,
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def _edits(self, text: str, runs: list[_Run], paragraph_texts: dict) -> list:
        newline = "\r\n" if "\r\n" in text else "\n"
        edits: list[tuple[int, int, str]] = []
        for idx, run in self._units(runs):
            new = paragraph_texts.get(str(idx))
            if new is None:
                continue
            lead, core, trail = _split_ws(run.text)
            if new == core:
                continue  # unchanged: the original bytes stay
            edits.append((run.start, run.end, _encode(lead + new + trail, run, newline)))
        return edits

    @staticmethod
    def _splice(text: str, edits: list, start: int = 0, end: int | None = None) -> str:
        end = len(text) if end is None else end
        out: list[str] = []
        pos = start
        for a, b, rep in sorted(edits):
            if a < start or b > end:
                continue
            out.append(text[pos:a])
            out.append(rep)
            pos = b
        out.append(text[pos:end])
        return "".join(out)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        text, bom, runs = self._read(original)
        return bom + self._splice(text, self._edits(text, runs, paragraph_texts)).encode("utf-8")

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() plus #729 (decision 13): a plural select (`{ $n -> [one] ...
        *[other] ... }`) gets a variant for each CLDR category the TARGET uses that it
        lacks (Polish few and many), holding the translated `[other]` variant, placed
        before the default variant. Fluent would fall back to the default variant at run
        time; writing them out gives translators and tools the same file every other
        plural-aware format gets (app/filters/plurals.py). A select whose keys are not
        all CLDR categories or numbers (`[male]`), or that has no `[other]`, is left alone."""
        text, bom, runs = self._read(original)
        edits = self._edits(text, runs, paragraph_texts)
        if target_locale:
            edits = edits + _variant_fill_edits(text, edits, target_locale, self._splice)
        return bom + self._splice(text, edits).encode("utf-8")
