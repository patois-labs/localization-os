"""JSON resource dialects (Batch 8): Flutter ARB, Chrome/WebExtension messages.json,
Structured JSON (Transifex) / FormatJS / React-Intl, and go-i18n.

The generic `json_filter` translates EVERY string value, which is wrong for these
dialects: ARB `@key` metadata, Chrome `description`/`placeholders`, FormatJS
`description`, Structured JSON `context`/`developer_comment` and go-i18n
`description`/`hash` are instructions for translators, not text to translate. This
module recognises the dialect by its shape (strictly: only files whose EVERY entry
fits the dialect switch away from the generic walk), translates only the message
strings, and rebuilds the file by splicing the translated strings into the ORIGINAL
bytes, so every untouched byte (indentation, key order, escapes, metadata) survives.

Message syntax per dialect:
  * ICU MessageFormat (ARB, FormatJS, Structured JSON): a message with `plural`,
    `selectordinal` or `select` arguments is split into one unit per branch (and one
    for any outer text, the argument shown as a protected `⟦xN⟧` token); `#` inside a
    plural branch is a protected token too. Simple arguments (`{name}`) stay as text:
    the shared placeholder guard (services/segment._TAG_RE) already protects them.
    On a localized build the plural branches follow the TARGET language's CLDR
    categories (Russian one/few/many/other; Japanese other): a missing category is
    added with the translated `other` text, a category the language does not use is
    dropped, explicit `=N` branches are always kept.
  * Chrome: `$name$` and `$1`..`$9` are protected tokens.
  * go-i18n: Go template actions `{{ ... }}` are protected tokens; a plural message
    object follows the target CLDR categories on a localized build, as above.

Specs (checked 2026-10-10):
  ARB      https://github.com/google/app-resource-bundle/wiki/ApplicationResourceBundleSpecification
  Chrome   https://developer.chrome.com/docs/extensions/reference/api/i18n
           https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Internationalization
  FormatJS https://formatjs.github.io/docs/getting-started/message-extraction
  ICU      https://unicode-org.github.io/icu/userguide/format_parse/messages/
  Structured JSON https://help.transifex.com/en/articles/6220899-structured-json
  go-i18n  https://github.com/nicksnyder/go-i18n (v2 message files)
"""

from __future__ import annotations

import json
import re
from json.decoder import scanstring

from app.filters.base import InputLimitError, check_segment_cap

_MAX_DEPTH = 200  # JSON nesting (resource files are a few levels deep)
_ICU_MAX_DEPTH = 40  # ICU argument nesting
CLDR_CATEGORIES = ("zero", "one", "two", "few", "many", "other")


# --------------------------------------------------------------------------------------
# A position-keeping JSON scanner. json.loads validates the text first; this walk then
# records where each string sits so build() can splice translations into the original.
# --------------------------------------------------------------------------------------
class Str:
    __slots__ = ("end", "start", "val")

    def __init__(self, value, start, end):
        self.val, self.start, self.end = value, start, end


class Obj:
    __slots__ = ("end", "map", "pairs", "start")

    def __init__(self, pairs, start, end):
        self.pairs, self.start, self.end = pairs, start, end
        self.map = dict(pairs)  # last key wins, like json.loads


class Arr:
    __slots__ = ("end", "items", "start")

    def __init__(self, items, start, end):
        self.items, self.start, self.end = items, start, end


class Other:
    __slots__ = ("end", "start")

    def __init__(self, start, end):
        self.start, self.end = start, end


_WS = re.compile(r"[ \t\n\r]*")
# NaN / Infinity / -Infinity: Python's json.loads accepts them, so the scanner must too
# (gate: a JSON upload holding NaN crashed with AttributeError once the sniff ran).
_LITERAL = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?|true|false|null|NaN|-?Infinity")


def scan(text: str):
    """Scan already-validated JSON text into Str/Obj/Arr/Other nodes with spans."""

    def value(i, depth):
        if depth > _MAX_DEPTH:
            raise InputLimitError("This JSON file is nested too deeply.")
        i = _WS.match(text, i).end()
        c = text[i]
        if c == '"':
            s, end = scanstring(text, i + 1)
            return Str(s, i, end), end
        if c == "{":
            start, pairs = i, []
            i = _WS.match(text, i + 1).end()
            if text[i] == "}":
                return Obj(pairs, start, i + 1), i + 1
            while True:
                i = _WS.match(text, i).end()
                key, i = scanstring(text, i + 1)
                i = _WS.match(text, i).end() + 1  # the colon
                node, i = value(i, depth + 1)
                pairs.append((key, node))
                i = _WS.match(text, i).end()
                if text[i] == "}":
                    return Obj(pairs, start, i + 1), i + 1
                i += 1  # the comma
        if c == "[":
            start, items = i, []
            i = _WS.match(text, i + 1).end()
            if text[i] == "]":
                return Arr(items, start, i + 1), i + 1
            while True:
                node, i = value(i, depth + 1)
                items.append(node)
                i = _WS.match(text, i).end()
                if text[i] == "]":
                    return Arr(items, start, i + 1), i + 1
                i += 1
        m = _LITERAL.match(text, i)
        return Other(i, m.end()), m.end()

    try:
        return value(0, 0)[0]
    except RecursionError as exc:
        raise InputLimitError("This JSON file is nested too deeply.") from exc


# --------------------------------------------------------------------------------------
# Dialect detection (strict: every entry must fit, or the generic walk is kept).
# --------------------------------------------------------------------------------------
_CHROME_KEYS = {"message", "description", "placeholders"}
_FORMATJS_KEYS = {"defaultMessage", "description", "id", "file", "start", "end", "line", "col"}
_STRUCTURED_KEYS = {"string", "context", "developer_comment", "character_limit"}
_GO_RESERVED = {"id", "description", "hash", "leftdelim", "rightdelim", "translation"} | set(
    CLDR_CATEGORIES
)


def _is_chrome(root) -> bool:
    if not isinstance(root, Obj) or not root.pairs:
        return False
    for _k, v in root.pairs:
        if not isinstance(v, Obj) or not isinstance(v.map.get("message"), Str):
            return False
        if not set(v.map) <= _CHROME_KEYS:
            return False
        ph = v.map.get("placeholders")
        if ph is not None and not (
            isinstance(ph, Obj) and all(isinstance(p, Obj) for _n, p in ph.pairs)
        ):
            return False
    return True


def _is_formatjs(root) -> bool:
    if not isinstance(root, Obj) or not root.pairs:
        return False
    return all(
        isinstance(v, Obj)
        and isinstance(v.map.get("defaultMessage"), Str)
        and set(v.map) <= _FORMATJS_KEYS
        for _k, v in root.pairs
    )


def _structured_leaf(v) -> bool:
    return (
        isinstance(v, Obj)
        and isinstance(v.map.get("string"), Str)
        and set(v.map) <= _STRUCTURED_KEYS
    )


def _is_structured(root) -> bool:
    if not isinstance(root, Obj) or not root.pairs:
        return False
    leaves = 0
    stack = [root]
    while stack:
        node = stack.pop()
        for _k, v in node.pairs:
            if _structured_leaf(v):
                leaves += 1
            elif isinstance(v, Obj) and v.pairs and "string" not in v.map:
                stack.append(v)
            else:
                return False
    return leaves > 0


def _go_message(v) -> bool:
    if not isinstance(v, Obj) or not v.pairs:
        return False
    keys = {k.lower() for k in v.map}
    if not keys <= _GO_RESERVED:
        return False
    if not keys & (set(CLDR_CATEGORIES) | {"translation"}):
        return False
    for k, val in v.pairs:
        lk = k.lower()
        if lk in CLDR_CATEGORIES and not isinstance(val, Str):
            return False
        if lk == "translation" and not (isinstance(val, Str) or _go_cats_obj(val)):
            return False
    return True


def _go_cats_obj(v) -> bool:
    return (
        isinstance(v, Obj)
        and bool(v.pairs)
        and all(k.lower() in CLDR_CATEGORIES and isinstance(x, Str) for k, x in v.pairs)
    )


def _go_plural(v) -> bool:
    """A go-i18n message object that carries plural forms (not just `other`)."""
    keys = {k.lower() for k in v.map}
    return "other" in keys and bool(keys & (set(CLDR_CATEGORIES) - {"other"}))


def _is_go(root) -> bool:
    if isinstance(root, Arr):  # the v1 array form: [{"id": ..., "translation": ...}]
        return bool(root.items) and all(
            isinstance(v, Obj)
            and isinstance(v.map.get("id"), Str)
            and "translation" in v.map
            and _go_message(v)
            for v in root.items
        )
    if not isinstance(root, Obj) or not root.pairs:
        return False
    plural_seen = False
    stack = [root]
    while stack:
        node = stack.pop()
        for _k, v in node.pairs:
            if isinstance(v, Str):
                continue
            if _go_message(v):
                plural_seen = plural_seen or _go_plural(v) or _go_cats_obj(v.map.get("translation"))
            elif isinstance(v, Obj) and v.pairs:
                stack.append(v)
            else:
                return False
    return plural_seen  # a flat id -> text file stays the generic JSON walk


def _is_arb_shape(root) -> bool:
    return isinstance(root, Obj) and isinstance(root.map.get("@@locale"), Str)


def detect(root) -> str:
    """The dialect of a `.json` file: chrome, formatjs, structured, go, arb or plain."""
    if _is_chrome(root):
        return "chrome"
    if _is_formatjs(root):
        return "formatjs"
    if _is_structured(root):
        return "structured"
    if _is_go(root):
        return "go"
    if _is_arb_shape(root):
        return "arb"
    return "plain"


# --------------------------------------------------------------------------------------
# Which string nodes are messages, in document order. Each entry: (Str node, syntax,
# plural_obj) where plural_obj is the go-i18n message object a category belongs to.
# --------------------------------------------------------------------------------------
def messages(root, dialect: str) -> list:
    out: list = []
    if dialect == "arb":
        for k, v in root.pairs:
            if not k.startswith("@") and isinstance(v, Str):
                out.append((v, "icu"))
    elif dialect == "chrome":
        for _k, v in root.pairs:
            out.append((v.map["message"], "chrome"))
    elif dialect == "formatjs":
        for _k, v in root.pairs:
            out.append((v.map["defaultMessage"], "icu"))
    elif dialect == "structured":
        _walk_structured(root, out)
    elif dialect == "go":
        items = root.items if isinstance(root, Arr) else None
        if items is not None:
            for v in items:
                _go_message_strings(v, out)
        else:
            _walk_go(root, out)
    return out


def _walk_structured(node, out):
    for _k, v in node.pairs:
        if _structured_leaf(v):
            out.append((v.map["string"], "icu"))
        else:
            _walk_structured(v, out)


def _walk_go(node, out):
    for _k, v in node.pairs:
        if isinstance(v, Str):
            out.append((v, "go"))
        elif _go_message(v):
            _go_message_strings(v, out)
        else:
            _walk_go(v, out)


def _go_message_strings(v, out):
    for k, val in v.pairs:
        lk = k.lower()
        if lk in CLDR_CATEGORIES:
            out.append((val, "go"))
        elif lk == "translation":
            if isinstance(val, Str):
                out.append((val, "go"))
            else:
                for _c, s in val.pairs:
                    out.append((s, "go"))


def go_plural_objects(root) -> list:
    """Every go-i18n object whose keys are plural categories (a message object with
    plural forms, or a v1 `translation` category object), in document order."""
    found: list = []

    def walk(node):
        if isinstance(node, Arr):
            for v in node.items:
                walk(v)
            return
        if not isinstance(node, Obj):
            return
        if _go_message(node):
            tr = node.map.get("translation")
            if _go_cats_obj(tr):
                found.append(tr)
            elif _go_plural(node):
                found.append(node)
            return
        for _k, v in node.pairs:
            walk(v)

    walk(root)
    return found


# --------------------------------------------------------------------------------------
# ICU MessageFormat: a small parser that keeps the RAW text of every piece so an
# unchanged message rebuilds byte-identically.
# --------------------------------------------------------------------------------------
class IcuError(ValueError):
    pass


class Text:
    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw


class Simple:
    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw


class Pound:
    __slots__ = ()
    raw = "#"


class Branch:
    __slots__ = ("mid", "msg", "selector", "trail")

    def __init__(self, selector, mid, msg, trail):
        self.selector, self.mid, self.msg, self.trail = selector, mid, msg, trail


class Complex:
    __slots__ = ("branches", "head", "kind")

    def __init__(self, head, kind, branches):
        self.head, self.kind, self.branches = head, kind, branches


_ICU_WS = re.compile(r"\s*")
_ICU_NAME = re.compile(r"[^\s,{}#'|]+")
_ICU_TYPE = re.compile(r"[A-Za-z]+")
_ICU_SELECTOR = re.compile(r"=-?\d+(?:\.\d+)?|[^\s{}]+")
_ICU_OFFSET = re.compile(r"offset\s*:\s*\d+\s*")
_COMPLEX = ("plural", "selectordinal", "select")


def parse_icu(s: str) -> list:
    parts, i = _icu_message(s, 0, 0, False, False)
    if i != len(s):
        raise IcuError("unbalanced brace")
    return parts


def _icu_message(s, i, depth, in_plural, nested):
    if depth > _ICU_MAX_DEPTH:
        raise IcuError("nested too deeply")
    parts: list = []
    buf: list = []
    n = len(s)

    def flush():
        if buf:
            parts.append(Text("".join(buf)))
            buf.clear()

    while i < n:
        c = s[i]
        if c == "{":
            flush()
            part, i = _icu_arg(s, i, depth)
            parts.append(part)
        elif c == "}":
            if not nested:
                raise IcuError("unbalanced brace")
            break
        elif c == "#" and in_plural:
            flush()
            parts.append(Pound())
            i += 1
        elif c == "'":
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt == "'":
                buf.append("''")
                i += 2
            elif nxt in ("{", "}", "|") or (nxt == "#" and in_plural):
                j = i + 1
                while j < n:
                    if s[j] == "'":
                        if j + 1 < n and s[j + 1] == "'":
                            j += 2
                            continue
                        break
                    j += 1
                buf.append(s[i : j + 1])
                i = j + 1
            else:
                buf.append(c)
                i += 1
        else:
            buf.append(c)
            i += 1
    flush()
    return parts, i


def _icu_arg(s, i, depth):
    start = i
    j = _ICU_WS.match(s, i + 1).end()
    m = _ICU_NAME.match(s, j)
    if not m:
        raise IcuError("argument without a name")
    j = _ICU_WS.match(s, m.end()).end()
    if j < len(s) and s[j] == "}":
        return Simple(s[start : j + 1]), j + 1
    if j >= len(s) or s[j] != ",":
        raise IcuError("bad argument")
    j = _ICU_WS.match(s, j + 1).end()
    m = _ICU_TYPE.match(s, j)
    if not m:
        raise IcuError("argument without a type")
    kind = m.group(0)
    j = _ICU_WS.match(s, m.end()).end()
    if j < len(s) and s[j] == "}":
        return Simple(s[start : j + 1]), j + 1
    if j >= len(s) or s[j] != ",":
        raise IcuError("bad argument")
    if kind not in _COMPLEX:  # number/date/time/... with a style: opaque, brace-balanced
        level, k = 1, j + 1
        while k < len(s) and level:
            if s[k] == "{":
                level += 1
            elif s[k] == "}":
                level -= 1
            elif s[k] == "'":
                close = s.find("'", k + 1)
                k = close if close != -1 else len(s)
            k += 1
        if level:
            raise IcuError("unclosed argument")
        return Simple(s[start:k]), k
    j = _ICU_WS.match(s, j + 1).end()
    if kind != "select":
        m = _ICU_OFFSET.match(s, j)
        if m:
            j = m.end()
    head = s[start:j]
    branches: list = []
    while True:
        if j >= len(s):
            raise IcuError("unclosed argument")
        if s[j] == "}":
            break
        m = _ICU_SELECTOR.match(s, j)
        if not m:
            raise IcuError("bad selector")
        sel = m.group(0)
        k = _ICU_WS.match(s, m.end()).end()
        if k >= len(s) or s[k] != "{":
            raise IcuError("selector without a message")
        sub, k = _icu_message(s, k + 1, depth + 1, kind != "select", True)
        if k >= len(s) or s[k] != "}":
            raise IcuError("unclosed branch")
        t = _ICU_WS.match(s, k + 1).end()
        branches.append(Branch(sel, s[m.end() : s.index("{", m.end())], sub, s[k + 1 : t]))
        j = t
    if not branches:
        raise IcuError("no branches")
    return Complex(head, kind, branches), j + 1


def _tok(n: int) -> str:
    return f"⟦x{n}⟧"


_TOKEN_RE = re.compile(r"⟦x(\d+)⟧")


def _translatable(text: str) -> bool:
    return any(c.isalnum() for c in _TOKEN_RE.sub("", text))


def _unit_text(parts) -> tuple[str, list]:
    """The unit text of one message level and the parts each ⟦xN⟧ token stands for."""
    out, toks = [], []
    for p in parts:
        if isinstance(p, (Pound, Complex)):
            toks.append(p)
            out.append(_tok(len(toks)))
        else:
            out.append(p.raw)
    return "".join(out), toks


def icu_units(parts, out: list) -> None:
    """Pre-order: this level's text (if any words), then each branch, recursively."""
    text, toks = _unit_text(parts)
    if _translatable(text):
        out.append(text)
    for p in toks:
        if isinstance(p, Complex):
            for b in p.branches:
                icu_units(b.msg, out)


def _cats(locale: str | None, ordinal: bool):
    from app.filters.plurals import target_categories

    return target_categories(locale, ordinal=ordinal)


def _render_branches(cx: Complex, rendered: list, target: str | None) -> str:
    """Rebuild a complex argument; plural kinds follow the target's CLDR categories."""
    pairs = list(zip(cx.branches, rendered, strict=True))
    cats = _cats(target, cx.kind == "selectordinal") if cx.kind != "select" else None
    if cats is not None and any(b.selector == "other" for b in cx.branches):
        other_b, other_txt = next((b, t) for b, t in pairs if b.selector == "other")
        kept = [(b, t) for b, t in pairs if b.selector.startswith("=") or b.selector in cats]
        have = {b.selector for b, _t in kept}
        missing = [c for c in cats if c not in have]
        if missing:
            idx = next(i for i, (b, _t) in enumerate(kept) if b.selector == "other")
            sep = kept[idx - 1][0].trail if idx > 0 else " "
            new = [(Branch(c, other_b.mid, None, sep or " "), other_txt) for c in missing]
            kept[idx:idx] = new
        pairs = kept
    body = "".join(f"{b.selector}{b.mid}{{{t}}}{b.trail}" for b, t in pairs)
    return cx.head + body + "}"


def icu_rebuild(parts, translations, target: str | None) -> str:
    """Rebuild one message level from the unit translations (an iterator of
    str-or-None in the same pre-order as icu_units)."""
    text, toks = _unit_text(parts)
    mine = next(translations) if _translatable(text) else None
    pieces: dict[int, str] = {}
    for n, p in enumerate(toks, 1):
        if isinstance(p, Complex):
            rendered = [icu_rebuild(b.msg, translations, target) for b in p.branches]
            pieces[n] = _render_branches(p, rendered, target)
        else:
            pieces[n] = "#"
    if mine is not None and _tokens_ok(mine, toks) and _icu_safe(mine):
        text = mine
    return _TOKEN_RE.sub(lambda m: pieces.get(int(m.group(1)), ""), text)


def _tokens_ok(text: str, toks: list) -> bool:
    found = [int(x) for x in _TOKEN_RE.findall(text)]
    if any(n < 1 or n > len(toks) for n in found):
        return False
    need = [n for n, p in enumerate(toks, 1) if isinstance(p, Complex)]
    return all(found.count(n) == 1 for n in need)


def _icu_safe(text: str) -> bool:
    """A translated text must not break the ICU structure it is spliced into."""
    try:
        parse_icu(_TOKEN_RE.sub("0", text))
    except IcuError:
        return False
    return True


# --------------------------------------------------------------------------------------
# Plain-message dialects: Chrome `$name$` / `$1`, go-i18n `{{ ... }}`.
# --------------------------------------------------------------------------------------
_CHROME_PH = re.compile(r"\$[A-Za-z0-9_@]+\$|\$[1-9]")
_GO_PH = re.compile(r"\{\{.*?\}\}", re.DOTALL)


def _protect(s: str, rx) -> tuple[str, list]:
    raws: list = []

    def sub(m):
        raws.append(m.group(0))
        return _tok(len(raws))

    return rx.sub(sub, s), raws


def _restore(text: str, raws: list) -> str | None:
    found = [int(x) for x in _TOKEN_RE.findall(text)]
    if any(n < 1 or n > len(raws) for n in found):
        return None
    return _TOKEN_RE.sub(lambda m: raws[int(m.group(1)) - 1], text)


# --------------------------------------------------------------------------------------
# Units and rebuild for a whole file.
# --------------------------------------------------------------------------------------
_PLAIN_RX = {"chrome": _CHROME_PH, "go": _GO_PH}


def _message_units(s: str, syntax: str) -> list:
    if syntax == "icu":
        try:
            parts = parse_icu(s)
        except IcuError:
            return [s] if _translatable(s) else []
        out: list = []
        icu_units(parts, out)
        return out
    text, _raws = _protect(s, _PLAIN_RX[syntax])
    return [text] if _translatable(text) else []


def file_units(root, dialect: str) -> list[str]:
    units: list[str] = []
    for node, syntax in messages(root, dialect):
        units.extend(_message_units(node.val, syntax))
        check_segment_cap(len(units))
    return units


def _rebuild_message(s: str, syntax: str, translations, target) -> str:
    if syntax == "icu":
        try:
            parts = parse_icu(s)
        except IcuError:
            if _translatable(s):
                t = next(translations)
                return s if t is None else t
            return s
        return icu_rebuild(parts, translations, target)
    text, raws = _protect(s, _PLAIN_RX[syntax])
    if not _translatable(text):
        return s
    t = next(translations)
    if t is None:
        return s
    restored = _restore(t, raws)
    return s if restored is None else restored


def rebuild(text: str, root, dialect: str, paragraph_texts: dict, target: str | None) -> str:
    """Splice translated messages into `text` (the original, BOM-less). With `target`,
    plural branches / go-i18n plural objects follow its CLDR categories and an ARB
    `@@locale` names it."""
    counter = [0]

    def translations():
        while True:
            i = counter[0]
            counter[0] += 1
            yield paragraph_texts.get(str(i))

    it = translations()
    edits: list = []  # (start, end, replacement)
    for node, syntax in messages(root, dialect):
        new = _rebuild_message(node.val, syntax, it, target)
        if new != node.val:
            edits.append((node.start, node.end, json.dumps(new, ensure_ascii=False)))
    if target and dialect == "go":
        edits = _go_plural_edits(text, root, edits, target)
    if target and dialect == "arb":
        edits.extend(_arb_suffix_plural_edits(text, root, edits, target))
        edits.append(_arb_locale_edit(text, root, target))
    # One join over non-overlapping edits in file order (gate: re-slicing the whole text
    # per edit was quadratic, 20 s for a 20000-message ARB).
    parts: list = []
    pos = 0
    for start, end, rep in sorted(edits, key=lambda e: e[0]):
        parts.append(text[pos:start])
        parts.append(rep)
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


_ARB_SUFFIX = re.compile(r"^([A-Za-z0-9_]+?)(Zero|One|Two|Few|Many|Other)$")


def _arb_suffix_plural_edits(text: str, root, edits: list, target: str) -> list:
    """#729 (decision 18, gate): suffix-style ARB plurals, one message per category
    (`itemCountOne`, `itemCountOther`) instead of one ICU plural, get a message for each
    CLDR category the TARGET uses that the file lacks (Czech `itemCountFew`,
    `itemCountMany`), holding the translated `...Other` message, inserted before it. The
    shared rule (app/filters/plurals.py). A group counts only with `Other` and at least one
    other category, all string messages; a key already present is never touched."""
    from app.filters.plurals import missing_categories

    groups: dict[str, dict[str, Str]] = {}
    for key, node in root.pairs:
        m = _ARB_SUFFIX.match(key)
        if m and isinstance(node, Str):
            groups.setdefault(m[1], {})[m[2].lower()] = node
    replaced = {(s, e): r for s, e, r in edits}
    out: list = []
    for stem, nodes in groups.items():
        other = nodes.get("other")
        if other is None or len(nodes) < 2:
            continue
        missing = [
            c for c in missing_categories(nodes, target) if stem + c.capitalize() not in root.map
        ]
        if not missing:
            continue
        key_at = text.rfind(json.dumps(stem + "Other"), root.start, other.start)
        if key_at < 0:
            continue
        value = replaced.get((other.start, other.end), text[other.start : other.end])
        line_start = text.rfind("\n", root.start, key_at)
        indent = text[line_start + 1 : key_at]
        sep = "\n" + indent if line_start != -1 and not indent.strip() else " "
        rep = "".join(f"{json.dumps(stem + c.capitalize())}: {value},{sep}" for c in missing)
        out.append((key_at, key_at, rep))
    return out


def _arb_locale_edit(text: str, root, target: str):
    loc = target.strip().replace("-", "_")
    node = root.map.get("@@locale")
    if isinstance(node, Str):
        return (node.start, node.end, json.dumps(loc, ensure_ascii=False))
    # No @@locale yet: insert one as the first entry, indented like the first key.
    if root.pairs:
        key_start = _WS.match(text, root.start + 1).end()  # the first key's opening quote
        line_start = text.rfind("\n", root.start, key_start)
        indent = text[line_start + 1 : key_start] if line_start != -1 else ""
        sep = "\n" + indent if line_start != -1 else " "
        rep = f'"@@locale": {json.dumps(loc, ensure_ascii=False)},{sep}'
        return (key_start, key_start, rep)
    return (root.start, root.end, "{" + f'"@@locale": {json.dumps(loc, ensure_ascii=False)}' + "}")


def _go_plural_edits(text: str, root, edits: list, target: str) -> list:
    """Rewrite each go-i18n plural object so its category keys match the target
    language: missing categories copy the translated `other`, unused ones go."""
    cats = _cats(target, False)
    if cats is None:
        return edits
    replaced = {(s, e): r for s, e, r in edits}
    for obj in go_plural_objects(root):
        keys = [k.lower() for k, _v in obj.pairs]
        cat_keys = [k for k in keys if k in CLDR_CATEGORIES]
        if "other" not in cat_keys:
            continue
        if set(cat_keys) == set(cats):
            continue
        other_node = next(v for k, v in obj.pairs if k.lower() == "other")
        other_json = replaced.get(
            (other_node.start, other_node.end), text[other_node.start : other_node.end]
        )
        indent, nl = _pair_indent(text, obj)
        entries: list = []
        inserted = False
        for k, v in obj.pairs:
            lk = k.lower()
            if lk in CLDR_CATEGORIES and lk not in cats:
                continue
            if lk == "other" and not inserted:
                for c in cats:
                    if c not in cat_keys:
                        entries.append(f'"{c}": {other_json}')
                inserted = True
            val = replaced.get((v.start, v.end), text[v.start : v.end])
            entries.append(f"{json.dumps(k, ensure_ascii=False)}: {val}")
        joiner = "," + (nl + indent if nl else " ")
        close_indent = _close_indent(text, obj) if nl else ""
        body = (
            (nl + indent if nl else "") + joiner.join(entries) + (nl + close_indent if nl else "")
        )
        inner = [(s, e, r) for s, e, r in edits if not (obj.start <= s and e <= obj.end)]
        edits = [*inner, (obj.start, obj.end, "{" + body + "}")]
        replaced = {(s, e): r for s, e, r in edits}
    return edits


def _pair_indent(text: str, obj) -> tuple[str, str]:
    first = obj.pairs[0][1]
    seg = text[obj.start : first.start]
    if "\n" not in seg:
        return "", ""
    line = seg[seg.rfind("\n") + 1 :]
    return line[: len(line) - len(line.lstrip(" \t"))], "\n"


def _close_indent(text: str, obj) -> str:
    """The whitespace before the object's closing brace on its own line ("" otherwise)."""
    nl = text.rfind("\n", obj.start, obj.end - 1)
    tail = text[nl + 1 : obj.end - 1] if nl != -1 else ""
    return tail if tail.strip() == "" else ""
