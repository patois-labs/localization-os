"""Windows .resjson filter (Windows Store / WinJS JavaScript app resources).

Format (Microsoft, "Quickstart: Translating UI resources (HTML)":
https://learn.microsoft.com/en-us/previous-versions/windows/apps/hh943060(v=win.10)):
a JSON object of resource id -> string, UTF-8. A key of the form `_<id>.comment` is the
translator note for `<id>`; it is metadata, never translated. Any other `_<id>.<attr>`
key (tools add `.source`, `.maxLength` and friends) is metadata too and is left alone.

Visual Studio's templates and many hand-edited files also carry JavaScript comments
(`// ...` and `/* ... */`), which plain JSON parsers refuse; they are accepted here and
kept byte for byte.

Build splices each translated string into the original text (JSON-escaped), so key
order, spacing, comments, the BOM and line endings all survive; untranslated strings
keep their original escapes. Nested objects are walked as well (context = the path).

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import json
import re

from app.filters.base import InputLimitError, register
from app.filters.json_filter import check_json_budget
from app.filters.kv_resource import (
    Entry,
    KeyValueFilter,
    ResourceEncoding,
    decode_with,
    sniff_bom,
)

_MAX_DEPTH = 64
_LIT = re.compile(r"true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")


def _is_meta(key: str) -> bool:
    return key.startswith("_") and "." in key[1:]


def _tokens(text: str):
    """Yield (kind, start, end): 'str' (start/end INCLUDE the quotes), 'lit' (number,
    true/false/null), or a punctuation character. Comments and blanks are skipped."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n﻿":
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j == -1:
                raise ValueError("This .resjson file has a /* comment that is never closed.")
            i = j + 2
        elif c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                elif text[j] == '"':
                    break
                else:
                    j += 1
            if j >= n:
                raise ValueError("This .resjson file has a string that is never closed.")
            yield "str", i, j + 1
            i = j + 1
        elif c in "{}[]:,":
            yield c, i, i + 1
            i += 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n,:{}[]"/':
                j += 1
            if j == i:
                raise ValueError(f"This .resjson file has an unexpected {c!r} at position {i}.")
            yield "lit", i, j
            i = j


def _jstr(text: str, start: int, end: int) -> str:
    try:
        return json.loads(text[start:end])
    except ValueError as exc:
        raise ValueError(
            f"This .resjson file has a bad string escape at position {start}."
        ) from exc


@register
class ResjsonFilter(KeyValueFilter):
    fmt = "resjson"
    exts = ()

    def _decode(self, data: bytes) -> tuple[str, ResourceEncoding]:
        bom = sniff_bom(data)
        if bom and bom[1] != "utf-8":
            enc = ResourceEncoding(codec=bom[1], bom=bom[0], label="utf-16")
        else:
            enc = ResourceEncoding(codec="utf-8", bom=bom[0] if bom else b"")
        return decode_with(data, enc), enc

    def _budget(self, text: str) -> None:
        super()._budget(text)
        check_json_budget(text, "This .resjson file")

    def _scan(self, text: str) -> list[Entry]:
        entries: list[Entry] = []
        comments: dict[tuple[str, str], str] = {}
        # stack of [kind, path, expecting]: kind 'obj'/'arr'; expecting 'key'/'value'/'sep'
        stack: list[list] = []
        pending_key: str | None = None
        seen_root = False
        for kind, s, e in _tokens(text):
            top = stack[-1] if stack else None
            if top is None:
                if seen_root:
                    raise ValueError("This .resjson file has text after the top-level object.")
                if kind != "{":
                    raise ValueError("A .resjson file must be one JSON object.")
                stack.append(["obj", "", "key"])
                seen_root = True
                continue
            tkind, path, expect = top
            if tkind == "obj" and expect == "key":
                if kind == "}":
                    stack.pop()
                    self._after_value(stack)
                    continue
                if kind != "str":
                    raise ValueError(f"This .resjson file expected a key at position {s}.")
                pending_key = _jstr(text, s, e)
                top[2] = "colon"
                continue
            if tkind == "obj" and expect == "colon":
                if kind != ":":
                    raise ValueError(f"This .resjson file expected ':' at position {s}.")
                top[2] = "value"
                continue
            if expect == "sep":
                if kind == ",":
                    top[2] = "key" if tkind == "obj" else "value"
                    continue
                if (kind == "}" and tkind == "obj") or (kind == "]" and tkind == "arr"):
                    stack.pop()
                    self._after_value(stack)
                    continue
                raise ValueError(f"This .resjson file expected ',' at position {s}.")
            # expecting a value
            if tkind == "arr" and kind == "]":
                stack.pop()
                self._after_value(stack)
                continue
            key = pending_key if tkind == "obj" else ""
            if kind in "{[":
                if len(stack) >= _MAX_DEPTH:
                    raise InputLimitError("This .resjson file is nested too deeply.")
                child = (
                    path + "/" + key if tkind == "obj" and path else key if tkind == "obj" else path
                )
                top[2] = "sep"
                stack.append(
                    ["obj" if kind == "{" else "arr", child, "key" if kind == "{" else "value"]
                )
                continue
            if kind == "str":
                value = _jstr(text, s, e)
                meta = tkind == "obj" and _is_meta(key)
                if meta and key.endswith(".comment"):
                    comments[(path, key[1 : -len(".comment")])] = value
                entries.append(
                    Entry(
                        key=key,
                        value=value,
                        start=s + 1,
                        end=e - 1,
                        context=path,
                        translatable=not meta,
                    )
                )
            elif kind != "lit" or not _LIT.fullmatch(text, s, e):
                raise ValueError(f"This .resjson file expected a value at position {s}.")
            top[2] = "sep"
        if stack or not seen_root:
            raise ValueError("This .resjson file ends before its object is closed.")
        for en in entries:
            en.comment = comments.get((en.context, en.key), "")
        return entries

    @staticmethod
    def _after_value(stack: list) -> None:
        if stack:
            stack[-1][2] = "sep"

    def _encode_value(self, new: str, entry: Entry, text: str, enc: ResourceEncoding) -> str:
        return json.dumps(new, ensure_ascii=False)[1:-1]
