"""JSON filter — for software i18n resource files (i18next, react-intl, etc.).

Only string *values* are translated; object keys, numbers, booleans, null, and
the nesting structure are preserved. Strings with no alphanumerics (ids, hex
colors, "{count}") are left alone — and crucially are counted identically in
parse() and build() so the unit index stays aligned.

Batch 8: a file shaped as a resource dialect (Chrome messages.json, FormatJS, Structured
JSON, go-i18n) translates only its messages, and Flutter .arb is its own fmt (ArbFilter
below); see json_filter_common. Every other JSON file keeps the generic walk above.
"""

import json
from collections import OrderedDict

from app import config
from app.filters import json_filter_common
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    check_unit_texts,
    newline_like,
    register,
)


def check_json_budget(text: str, what: str = "This JSON file") -> None:
    """The one pre-parse budget for every JSON ingest (document upload and key-file
    import, #571 and #576). Raises InputLimitError before json.loads builds a tree.

    #571: json.loads builds EVERY string before any unit count exists, so a 100 MB file of
    tiny strings costs gigabytes. Quote characters bound the string count at C speed (two
    per string, four per key/value pair): refuse far above any real resource file (50000
    translatable strings need about 200000 quotes).
    #571 gate: values that are not strings (numbers, true/false/null, {} and []) carry no
    quotes, so 100 MB of `{},` built 33 million objects (gigabytes) before any unit
    existed. Every value after the first in a container needs a comma and every container
    a bracket: twenty per allowed segment is far above any resource file."""
    if text.count('"') > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"{what} has far more strings than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )
    if text.count(",") + text.count("[") + text.count("{") > 20 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"{what} has far more values than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


def _decode(data: bytes) -> str:
    """Decode an uploaded JSON resource file HONESTLY, matching the keys-import
    strictness (services/key_files.parse_json_keys uses the same `utf-8-sig`).

    utf-8-sig (P-B3): a BOM-prefixed file -- common from Windows editors -- has its
    leading BOM stripped and parses cleanly, instead of being refused by a plain
    `utf-8` decode.

    STRICT, no "replace" (P-B2): a non-UTF-8 file (e.g. latin-1 `café`) is REFUSED
    with an honest error rather than silently decoded with U+FFFD replacement
    characters and STORED as permanent mojibake (`caf?`). A ValueError here is caught
    by the upload route (-> a polite "parse" refusal) exactly like a JSONDecodeError."""
    if not isinstance(data, (bytes, bytearray)):
        return str(data)
    try:
        return bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "JSON file is not valid UTF-8 (byte 0x%02x at position %d could not be "  # noqa: UP031
            "decoded); re-save the file as UTF-8." % (data[exc.start], exc.start)
        ) from exc


def _is_translatable(s: str) -> bool:
    return any(c.isalnum() for c in s)


def _collect(obj, out: list, capped: bool = False):
    if isinstance(obj, dict):
        for v in obj.values():
            _collect(v, out, capped)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, out, capped)
    elif isinstance(obj, str) and _is_translatable(obj):
        out.append(obj)
        if capped:
            check_segment_cap(len(out))  # #571: stop before collecting the rest


def _replace(obj, repls: dict, ctr: list):
    if isinstance(obj, dict):
        return OrderedDict((k, _replace(v, repls, ctr)) for k, v in obj.items())
    if isinstance(obj, list):
        return [_replace(v, repls, ctr) for v in obj]
    if isinstance(obj, str) and _is_translatable(obj):
        i = ctr[0]
        ctr[0] += 1
        return repls.get(str(i), obj)
    return obj


_I18NEXT_SUFFIX = ("zero", "one", "two", "few", "many", "other")


def _i18next_fill(obj, locale: str, depth: int = 0):
    """#729 (decision 18): i18next v4 plural keys (`item_one`, `item_other`; ordinals
    `place_ordinal_one` ...) get every CLDR category the TARGET uses that the object lacks
    (Polish `item_few`, `item_many`), each filled with the translated `_other` value and
    placed before it, so i18next shows the translation for every count (it falls back to
    the source language for a missing key). The rule is the one every plural-aware filter
    shares (app/filters/plurals.py). A group counts only when it has `_other` and at least
    one other category key, all string values; nothing is removed."""
    from app.filters.plurals import missing_categories

    if depth > 200:
        return obj  # deeper than any resource file: left as built
    if isinstance(obj, list):
        return [_i18next_fill(v, locale, depth + 1) for v in obj]
    if not isinstance(obj, dict):
        return obj
    groups: dict[tuple[str, bool], set[str]] = {}
    for key, val in obj.items():
        if not isinstance(key, str) or not isinstance(val, str) or "_" not in key:
            continue
        stem, _, cat = key.rpartition("_")
        if cat not in _I18NEXT_SUFFIX or not stem:
            continue
        ordinal = stem.endswith("_ordinal")
        if ordinal:
            stem = stem[: -len("_ordinal")]
        groups.setdefault((stem, ordinal), set()).add(cat)
    adds: dict[str, list[str]] = {}
    for (stem, ordinal), cats in groups.items():
        if len(cats) < 2:
            continue
        other_key = f"{stem}{'_ordinal' if ordinal else ''}_other"
        for cat in missing_categories(cats, locale, ordinal=ordinal):
            new_key = f"{stem}{'_ordinal' if ordinal else ''}_{cat}"
            if new_key not in obj:
                adds.setdefault(other_key, []).append(new_key)
    out = OrderedDict()
    for key, val in obj.items():
        for new_key in adds.get(key, ()):
            out[new_key] = val
        out[key] = _i18next_fill(val, locale, depth + 1)
    return out


@register
class JsonFilter(Filter):
    fmt = "json"

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text = _decode(data)
        check_json_budget(text)
        try:
            obj = json.loads(text, object_pairs_hook=OrderedDict)
        except RecursionError as exc:  # deeply nested arrays/objects
            raise InputLimitError("This JSON file is nested too deeply.") from exc
        # Batch 8: a resource dialect (Chrome messages.json, FormatJS, Structured JSON,
        # go-i18n, ARB-shaped) translates only its message strings. Anything else keeps
        # the generic walk below, unchanged.
        root, dialect = _sniff(text)
        if dialect != "plain":
            return dialect_parse(root, dialect, "json")
        strings: list[str] = []
        try:
            _collect(obj, strings, capped=True)
        except RecursionError as exc:
            raise InputLimitError("This JSON file is nested too deeply.") from exc
        units = [TextUnit(p_index=i, text=t) for i, t in enumerate(strings)]
        skeleton = {
            "format": "json",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict, dialect: str | None = None) -> bytes:
        """`dialect`: the one the document was imported with (skeleton "dialect"; a
        skeleton without one was imported by the generic walk, pass "plain"). None sniffs."""
        text = _decode(original)
        obj = json.loads(text, object_pairs_hook=OrderedDict)
        if dialect != "plain":
            root, found = _sniff(text)
            if found != "plain" and dialect in (None, found):
                return dialect_build(original, root, found, paragraph_texts, None)
        spliced = _splice_plain(text, paragraph_texts)
        if spliced is not None:
            bom = b"\xef\xbb\xbf" if bytes(original).startswith(b"\xef\xbb\xbf") else b""
            return bom + spliced.encode("utf-8")
        rebuilt = _replace(obj, paragraph_texts, [0])
        return (json.dumps(rebuilt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def build_localized(
        self,
        original: bytes,
        paragraph_texts: dict,
        target_locale: str | None = None,
        dialect: str | None = None,
    ) -> bytes:
        """A dialect file follows the target's CLDR plural categories (ICU plural
        branches, go-i18n plural objects) and an ARB-shaped file's @@locale names the
        target. Generic JSON has no language declaration: build() plus the target's
        missing i18next plural keys (#729, `_i18next_fill`)."""
        if dialect != "plain" and target_locale:
            text = _decode(original)
            json.loads(text)  # validate before the position scan
            root, found = _sniff(text)
            if found != "plain" and dialect in (None, found):
                return dialect_build(original, root, found, paragraph_texts, target_locale)
        out = self.build(original, paragraph_texts, dialect=dialect)
        bom = b"\xef\xbb\xbf" if out.startswith(b"\xef\xbb\xbf") else b""
        body = out[len(bom) :]
        if not target_locale or not body.lstrip().startswith(b"{"):
            return out
        # #729: generic JSON (i18next) plural keys follow the target's CLDR categories
        parsed = json.loads(body, object_pairs_hook=OrderedDict)
        filled = _i18next_fill(parsed, target_locale)
        if filled == parsed:
            return out  # B-bytes gate: nothing to add, so the spliced build stands as is
        dumped = (json.dumps(filled, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        return bom + newline_like(bytes(original), dumped)


def _splice_plain(text: str, paragraph_texts: dict) -> str | None:
    """B-bytes: the generic walk's translations spliced into the original text, so the
    layout (compact or indented), line endings, key order, escapes of untouched strings
    and duplicate keys stay byte for byte. Walks exactly as _collect does (the last of a
    duplicate key is the value json.loads keeps). None when the scan refuses the file
    (nested too deeply): the caller then re-serializes, as before."""
    try:
        root = json_filter_common.scan(text)
    except InputLimitError:
        return None
    spans: list = []

    def walk(node):
        if isinstance(node, json_filter_common.Obj):
            for v in node.map.values():
                walk(v)
        elif isinstance(node, json_filter_common.Arr):
            for v in node.items:
                walk(v)
        elif isinstance(node, json_filter_common.Str) and _is_translatable(node.val):
            spans.append(node)

    try:
        walk(root)
    except RecursionError:
        return None
    edits = []
    for i, node in enumerate(spans):
        new = paragraph_texts.get(str(i))
        if new is not None and new != node.val:
            edits.append((node, new))
    # B-bytes gate: a duplicate key is walked at its FIRST key's position (dict order) but
    # its span is the LAST value's, so walk order is not file order: splice by position.
    edits.sort(key=lambda e: e[0].start)
    out: list[str] = []
    pos = 0
    for node, new in edits:
        out.append(text[pos : node.start])
        out.append(json.dumps(new, ensure_ascii=False))
        pos = node.end
    out.append(text[pos:])
    return "".join(out)


def _sniff(text: str):
    """(position tree, dialect) of valid JSON text. A file nested deeper than any
    resource dialect allows is generic JSON (its walk is unchanged)."""
    try:
        root = json_filter_common.scan(text)
    except InputLimitError:
        return None, "plain"
    return root, json_filter_common.detect(root)


def dialect_parse(root, dialect: str, fmt: str) -> ParseResult:
    """Units of a resource-dialect file (json_filter_common): message strings only."""
    texts = json_filter_common.file_units(root, dialect)
    check_unit_texts(texts)
    units = [TextUnit(p_index=i, text=t) for i, t in enumerate(texts)]
    skeleton = {
        "format": fmt,
        "dialect": dialect,
        "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
    }
    return ParseResult(skeleton=skeleton, units=units)


def dialect_build(original: bytes, root, dialect: str, paragraph_texts: dict, target) -> bytes:
    """Splice translations into the original bytes (a UTF-8 BOM and the layout kept)."""
    text = _decode(original)
    out = json_filter_common.rebuild(text, root, dialect, paragraph_texts, target)
    bom = b"\xef\xbb\xbf" if bytes(original).startswith(b"\xef\xbb\xbf") else b""
    return bom + out.encode("utf-8")


# --------------------------------------------------------------------------------------
# Flutter / Application Resource Bundle (.arb), Batch 8. Lives here (not its own module)
# so the JSON family stays one responsibility for the kernel narrowness bound.
#
# ARB is JSON with conventions: a top-level object whose plain keys hold ICU
# MessageFormat messages, `@key` objects hold that message's metadata (description,
# placeholders, type, context) and `@@` keys are global attributes (`@@locale`,
# `@@last_modified`, `@@author`, `@@x-...`). Only the messages are translated; every `@`
# key is kept byte for byte. A localized build switches `@@locale` to the target (added
# as the first entry when the source has none) and gives each ICU plural the target
# language's CLDR categories. Parsing and rebuilding live in json_filter_common.
# Spec: https://github.com/google/app-resource-bundle/wiki/ApplicationResourceBundleSpecification
# Flutter: https://docs.flutter.dev/ui/accessibility-and-internationalization/internationalization
# --------------------------------------------------------------------------------------
def _arb_root(text: str):
    try:
        json.loads(text)
    except RecursionError as exc:
        raise InputLimitError("This ARB file is nested too deeply.") from exc
    root = json_filter_common.scan(text)
    if not isinstance(root, json_filter_common.Obj):
        raise ValueError("An ARB file must be a JSON object of messages.")
    return root


@register
class ArbFilter(Filter):
    fmt = "arb"

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text = _decode(data)
        check_json_budget(text, "This ARB file")
        return dialect_parse(_arb_root(text), "arb", "arb")

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        root = _arb_root(_decode(original))
        return dialect_build(original, root, "arb", paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        root = _arb_root(_decode(original))
        return dialect_build(original, root, "arb", paragraph_texts, target_locale)
