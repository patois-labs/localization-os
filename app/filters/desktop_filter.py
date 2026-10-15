"""Freedesktop Desktop Entry filter (.desktop launchers, Desktop Entry Specification 1.5).

What is translated
  * In the ``[Desktop Entry]`` group and every ``[Desktop Action <id>]`` group: the
    UNLOCALIZED ``Name``, ``GenericName``, ``Comment`` and ``Keywords`` keys (all of type
    localestring). ``Keywords`` is a semicolon list: each item is its own unit.
  * Never: localized lines (``Name[de]=...``), any other key (Exec, Icon, Categories,
    MimeType, X-* extensions), comments, any other group.

Values follow the spec's escaping: ``\\s`` (space), ``\\n``, ``\\t``, ``\\r``, ``\\\\`` and,
inside a list, ``\\;``. Units carry the unescaped text; translations are escaped the same
way on build (a leading or trailing space is written as ``\\s``, because readers strip
whitespace around the value).

build() is the byte-faithful rebuild: the changed values are spliced into the original
lines, everything else is identical. build_localized() (every export) leaves the source
lines alone and adds ``Key[ll_CC]=translation`` for the TARGET locale right after the
source key, or replaces that line in place when the file already has one. Other locales'
lines, comments, groups and line endings are untouched. The BCP 47 target is written as
a POSIX locale: ``fr-FR`` -> ``fr_FR``, ``sr-Latn`` -> ``sr@latin``, ``zh-Hans`` -> ``zh_CN``.

Encoding: the spec says UTF-8. UTF-8 (a BOM is kept) is read; anything else is refused.

Spec: https://specifications.freedesktop.org/desktop-entry/latest/ (basic-format.html,
value-types.html, localized-keys.html, recognized-keys.html, extra-actions.html).

Documented in: docs/architecture/translation-pipeline.md
"""

import re
from dataclasses import dataclass

from app.filters.base import (
    EmptyDocumentError,
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    UnsupportedEncodingError,
    check_line_budget,
    check_segment_cap,
    check_unit_texts,
    register,
)
from app.filters.kv_resource import KeyValueRecord  # Batch 1's shared record type

TRANSLATABLE_KEYS = ("Name", "GenericName", "Comment", "Keywords")
LIST_KEYS = ("Keywords",)
_UTF8_BOM = b"\xef\xbb\xbf"
_LINE = re.compile(r"[^\n]*\n|[^\n]+\Z")
_KEY = re.compile(r"\A([A-Za-z0-9-]+)(?:\[([^\]\s]+)\])?\Z")
_UNESCAPE = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
_MAX_BYTES = 8 * 1024 * 1024  # a launcher file is a few KB; refuse absurd uploads early


class DesktopParseError(ValueError):
    """A .desktop upload that is not a Desktop Entry file. Caught by the upload routes as a
    polite parse refusal."""


@dataclass
class _Entry:
    group: str
    key: str
    line_start: int  # offset of the line in the text
    line_end: int  # offset just past the line content (before the line break)
    eol: str  # the line break after the content ("", "\n" or "\r\n")
    v_start: int  # value core span (whitespace around the value excluded)
    v_end: int
    raw_items: list  # raw (escaped) items; one item for a plain string
    items: list  # unescaped items
    trailing_sep: bool  # list value ended with ';'
    comment: str


@dataclass
class _Walk:
    text: str
    bom: bool
    entries: list  # translatable source entries, file order
    units: list  # (entry index, item index) per unit
    localized: dict  # (group, key, locale) -> (line_start, line_end)


def _translatable_group(group: str) -> bool:
    return group == "Desktop Entry" or group.startswith("Desktop Action ")


def _unescape(raw: str) -> str:
    out, i, n = [], 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in _UNESCAPE:
                out.append(_UNESCAPE[nxt])
            elif nxt == ";":
                out.append(";")
            else:  # unknown escape: kept as written
                out.append(c + nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _split_list(raw: str) -> tuple[list, bool]:
    """Split a list value on unescaped ';'. Returns (raw items, ended with ';')."""
    items, cur, i, n = [], [], 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            cur.append(raw[i : i + 2])
            i += 2
            continue
        if c == ";":
            items.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    tail = "".join(cur)
    trailing = tail == "" and bool(items)
    if not trailing:
        items.append(tail)
    return items, trailing


def _escape(s: str, in_list: bool) -> str:
    s = s.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    if in_list:
        s = s.replace(";", "\\;")
    # readers strip whitespace around the value / list item: keep edge spaces as \s
    lead = len(s) - len(s.lstrip(" "))
    s = "\\s" * lead + s[lead:]
    trail = len(s) - len(s.rstrip(" "))
    if trail:
        s = s[: len(s) - trail] + "\\s" * trail
    return s


def _decode(raw: bytes) -> tuple[str, bool]:
    raw = bytes(raw)
    if len(raw) > _MAX_BYTES:
        raise InputLimitError(
            f"This .desktop file is larger than {_MAX_BYTES // (1024 * 1024)} MB; "
            "a Desktop Entry file is a few KB."
        )
    bom = raw.startswith(_UTF8_BOM)
    try:
        text = raw[3 if bom else 0 :].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedEncodingError(
            "This .desktop file is not valid UTF-8 (the Desktop Entry Specification requires "
            f"UTF-8; byte at position {exc.start}); re-save the file as UTF-8."
        ) from exc
    if "\x00" in text:
        raise UnsupportedEncodingError(
            "This .desktop file looks like a binary file, not text; re-save it as UTF-8."
        )
    check_line_budget(text)
    return text, bom


def _walk(raw: bytes) -> _Walk:
    text, bom = _decode(raw)
    entries: list[_Entry] = []
    units: list[tuple[int, int]] = []
    localized: dict = {}
    group = None
    seen_desktop_entry = False
    comments: list[str] = []
    for m in _LINE.finditer(text):
        line = m.group(0)
        eol = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
        content = line[: len(line) - len(eol)]
        start = m.start()
        stripped = content.strip()
        if not stripped:
            comments = []
            continue
        if stripped.startswith("#"):
            comments.append(stripped[1:].strip())
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            group = stripped[1:-1]
            seen_desktop_entry = seen_desktop_entry or group == "Desktop Entry"
            comments = []
            continue
        note, comments = "\n".join(comments), []
        eq = content.find("=")
        if eq < 0 or group is None or not _translatable_group(group):
            continue  # not a key line we touch (kept byte for byte)
        km = _KEY.match(content[:eq].strip())
        if not km:
            continue
        key, loc = km.group(1), km.group(2)
        if loc is not None:
            localized.setdefault((group, key, loc), (start, start + len(content)))
            continue
        if key not in TRANSLATABLE_KEYS:
            continue
        rest = content[eq + 1 :]
        core = rest.strip(" \t")
        v_start = start + eq + 1 + (len(rest) - len(rest.lstrip(" \t")))
        v_end = v_start + len(core)
        if key in LIST_KEYS:
            raw_items, trailing = _split_list(core)
        else:
            raw_items, trailing = [core], False
        items = [_unescape(r) for r in raw_items]
        e_idx = len(entries)
        entries.append(
            _Entry(
                group,
                key,
                start,
                start + len(content),
                eol,
                v_start,
                v_end,
                raw_items,
                items,
                trailing,
                note,
            )
        )
        for i_idx, item in enumerate(items):
            if item.strip():
                units.append((e_idx, i_idx))
                check_segment_cap(len(units))
    if not seen_desktop_entry:
        raise DesktopParseError(
            "This file is not a Desktop Entry file: it has no [Desktop Entry] group."
        )
    return _Walk(text, bom, entries, units, localized)


def _value_for(walk: _Walk, e_idx: int, paragraph_texts: dict, unit_of: dict) -> str | None:
    """The rebuilt (escaped) value of one entry, or None when no unit of it is in the map."""
    e = walk.entries[e_idx]
    in_list = e.key in LIST_KEYS
    touched = False
    parts = []
    for i_idx, raw_item in enumerate(e.raw_items):
        u = unit_of.get((e_idx, i_idx))
        new = paragraph_texts.get(str(u)) if u is not None else None
        if new is None:
            parts.append(raw_item)
        else:
            touched = True
            parts.append(_escape(new, in_list))
    if not touched:
        return None
    if in_list:
        return ";".join(parts) + (";" if e.trailing_sep else "")
    return parts[0]


def posix_locale(locale: str) -> str:
    """BCP 47 (`fr-FR`, `sr-Latn-RS`, `zh-Hant`) -> the POSIX form Desktop Entry keys use
    (`fr_FR`, `sr_RS@latin`, `zh_TW`): lang_COUNTRY@MODIFIER (localized-keys.html)."""
    parts = [p for p in re.split(r"[-_]", (locale or "").strip()) if p]
    if not parts:
        return ""
    lang = parts[0].lower()
    script = region = modifier = ""
    for p in parts[1:]:
        if len(p) == 4 and p.isalpha() and not script:
            script = p.title()
        elif (len(p) == 2 and p.isalpha()) or (len(p) == 3 and p.isdigit()):
            region = region or p.upper()
        elif p.startswith("@"):
            modifier = p[1:]
        elif len(p) >= 5 and not modifier:
            modifier = p.lower()  # a variant such as ca-ES-valencia -> ca_ES@valencia
    if lang == "zh" and script and not region:
        region = {"Hans": "CN", "Hant": "TW"}.get(script, "")
        script = ""
    if lang == "zh":
        script = ""
    if script and not modifier:
        modifier = {"Latn": "latin", "Cyrl": "cyrillic"}.get(script, "")
    out = lang + (f"_{region}" if region else "")
    return out + (f"@{modifier}" if modifier else "")


@register
class DesktopFilter(Filter):
    fmt = "desktop"

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        walk = _walk(data)
        texts = [walk.entries[e].items[i] for e, i in walk.units]
        check_unit_texts(texts)
        if not texts:
            raise EmptyDocumentError(
                "This .desktop file has no Name, GenericName, Comment or Keywords value to "
                "translate."
            )
        units = [TextUnit(p_index=n, text=t) for n, t in enumerate(texts)]
        skeleton = {
            "format": "desktop",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        walk = _walk(original)
        unit_of = {pos: n for n, pos in enumerate(walk.units)}
        # build(): only units whose text really changed
        changed = {
            k: v
            for k, v in paragraph_texts.items()
            if k.isdigit()
            and int(k) < len(walk.units)
            and v != walk.entries[walk.units[int(k)][0]].items[walk.units[int(k)][1]]
        }
        text, out, last = walk.text, [], 0
        for e_idx, e in enumerate(walk.entries):
            value = _value_for(walk, e_idx, changed, unit_of)
            if value is None:
                continue
            out.append(text[last : e.v_start])
            out.append(value)
            last = e.v_end
        if not out:
            return bytes(original)
        out.append(text[last:])
        return (_UTF8_BOM if walk.bom else b"") + "".join(out).encode("utf-8")

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """Add or replace ``Key[ll_CC]=`` for the target locale after each source key that
        has a translation; the source lines and every other locale stay as they are."""
        loc = posix_locale(target_locale or "")
        if not loc:
            return self.build(original, paragraph_texts)
        walk = _walk(original)
        unit_of = {pos: n for n, pos in enumerate(walk.units)}
        text = walk.text
        edits: list[tuple[int, int, str]] = []  # (start, end, replacement), file order
        for e_idx, e in enumerate(walk.entries):
            value = _value_for(walk, e_idx, paragraph_texts, unit_of)
            if value is None:
                continue
            line = f"{e.key}[{loc}]={value}"
            existing = walk.localized.get((e.group, e.key, loc))
            if existing:
                edits.append((existing[0], existing[1], line))
            else:
                eol = e.eol or _first_eol(text)
                if e.eol:
                    edits.append((e.line_end + len(e.eol), e.line_end + len(e.eol), line + eol))
                else:  # the source key is the last line, with no line break
                    edits.append((e.line_end, e.line_end, eol + line))
        if not edits:
            return bytes(original)
        edits.sort(key=lambda t: (t[0], t[1]))
        out, last = [], 0
        for start, end, rep in edits:
            out.append(text[last:start])
            out.append(rep)
            last = max(last, end)
        out.append(text[last:])
        return (_UTF8_BOM if walk.bom else b"") + "".join(out).encode("utf-8")

    def records(self, data: bytes) -> list[KeyValueRecord]:
        """Key-based view (Batch 9): one record per unit. Keys are ``<group>/<Key>``
        (``Desktop Entry/Name``, ``Desktop Action new-window/Name``); a Keywords item is
        ``<group>/Keywords[<n>]``. ``context`` is the group, ``comment`` the ``#`` lines
        directly above the key."""
        walk = _walk(data)
        records: list[KeyValueRecord] = []
        seen: set = set()
        for n, (e_idx, i_idx) in enumerate(walk.units):
            e = walk.entries[e_idx]
            key = f"{e.group}/{e.key}" + (f"[{i_idx}]" if e.key in LIST_KEYS else "")
            records.append(
                KeyValueRecord(
                    key=key,
                    value=e.items[i_idx],
                    comment=e.comment,
                    context=e.group,
                    unit_index=n,
                    duplicate=key in seen,
                )
            )
            seen.add(key)
        return records


def _first_eol(text: str) -> str:
    i = text.find("\n")
    return "\r\n" if i > 0 and text[i - 1] == "\r" else "\n"
