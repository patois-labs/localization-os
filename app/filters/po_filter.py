"""gettext PO filter — the classic software-localization format.

parse(): each entry's ``msgid`` (and ``msgid_plural`` if present) becomes a
translatable unit. The header entry (msgid "") and all comments are preserved.

build(): write supplied translations into msgstr or the first two indexed slots,
preserving other plural slots and clearing fuzzy/previous-string comments only on
updated entries. Localized builds keep Language and Plural-Forms consistent.
"""

import re

from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextEnvelope,
    TextUnit,
    check_segment_cap,
    decode_text_lossless,
    encode_like,
    encode_text_lossless,
    note_source_encoding,
    read_text_upload,
    register,
    text_envelope,
)

_ESC = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
_KEYWORDS = ("msgctxt", "msgid_plural", "msgid", "msgstr")
# The `Language: xx` field inside the (unescaped) header msgstr.
_LANGUAGE_FIELD_RE = re.compile(r"(?m)^Language:[^\n]*$")
# Any `Name-Like-This: value` line: what a real header msgstr is made of.
_HEADER_FIELD_RE = re.compile(r"(?m)^[A-Za-z][A-Za-z-]*: ")


# A backslash and the character after it (any character, newline included), matched
# left to right without overlap: exactly the pairs the old per-character loop consumed.
_ESCAPE_PAIR_RE = re.compile(r"\\(.)", re.S)


def _unescape(s: str) -> str:
    # #571 gate: the per-character Python loop cost about 0.3 microseconds a character,
    # so one 100 MB msgid line held the interpreter for half a minute. Text without a
    # backslash is returned as is; escapes are replaced at C speed. A trailing lone
    # backslash stays, as before.
    if "\\" not in s:
        return s
    return _ESCAPE_PAIR_RE.sub(lambda m: _ESC.get(m.group(1), m.group(1)), s)


def _escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )


def _qval(line: str) -> str:
    a, b = line.find('"'), line.rfind('"')
    return _unescape(line[a + 1 : b]) if a != -1 and b > a else ""


def _kw(line: str):
    stripped = line.lstrip()
    for kw in _KEYWORDS:
        if stripped.startswith(kw) and (stripped[len(kw) : len(kw) + 1] in (" ", "[", '"')):
            return kw
    return None


def _msgstr_done(entry: dict) -> bool:
    """True once an entry has a msgstr (or any plural slot) set.

    A comment, msgctxt or msgid that follows a completed msgstr begins a new entry even
    with no blank line between the two blocks (valid for GNU gettext). msgid_plural and
    msgstr lines stay in the current entry, so they never trigger this.
    """
    if entry["msgstr"] is not None:
        return True
    return any(k.startswith("msgstr") and k[6:].isdigit() for k in entry)


def _parse_entries(data: bytes) -> list[dict]:
    text = (
        decode_text_lossless(data, sig=True, declared=True)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    entries: list[dict] = []
    cur = {
        "comments": [],
        "msgctxt": None,
        "msgid": "",
        "msgid_plural": None,
        "msgstr": None,
        "has_kw": False,
        "msgid_ok": False,
    }
    field = None  # which key the continuation strings attach to
    # #571 gate: continuation strings are collected in lists and joined once per entry.
    # Appending each one to the stored string copied the whole value every line, so a
    # 4 MB msgid of 60-character continuation lines took 49 s (quadratic).
    parts: dict[str, list[str]] = {}
    # B-bytes: the line span of the entry being read (first, last line index), so an
    # entry nothing changed is written back as its original lines (see _render).
    span: list[int] = []

    def settle():
        for key, pieces in parts.items():
            cur[key] = "".join(pieces)
        parts.clear()

    def flush():
        nonlocal cur
        settle()
        # An entry is kept when it has comments OR any keyword line. Keying on a
        # non-empty msgid (as this once did) dropped a header (msgid "") that had no
        # comment above it, losing charset, Plural-Forms and Language.
        if cur["comments"] or cur["has_kw"]:
            if span:
                cur["_span"] = (span[0], span[-1])
                cur["_was"] = _fields(cur)
            entries.append(cur)
        span.clear()
        cur = {
            "comments": [],
            "msgctxt": None,
            "msgid": "",
            "msgid_plural": None,
            "msgstr": None,
            "has_kw": False,
            "msgid_ok": False,
        }

    for n, raw in enumerate(text.split("\n")):
        line = raw.rstrip("\n")
        if not line.strip():
            flush()
            field = None
            continue
        # A new entry may begin without a blank line: a comment, msgctxt or msgid that
        # follows a completed msgstr starts the next entry. Keying only on blank lines let
        # a second msgid overwrite the first (dropping the entry) and glued a following
        # comment onto the previous entry.
        boundary = _msgstr_done(cur)
        if line.lstrip().startswith("#"):
            if boundary:
                flush()
                field = None
            cur["comments"].append(line)
            span.append(n)
            continue
        kw = _kw(line)
        if kw in ("msgctxt", "msgid") and boundary:
            flush()
            field = None
        span.append(n)
        if kw:
            cur["has_kw"] = True
            settle()  # a keyword line starts a new value; earlier pieces are final
        if kw == "msgctxt":
            cur["msgctxt"] = _qval(line)
            field = "msgctxt"
        elif kw == "msgid_plural":
            cur["msgid_plural"] = _qval(line)
            field = "msgid_plural"
        elif kw == "msgid":
            cur["msgid"] = _qval(line)
            cur["msgid_ok"] = line.find('"') < line.rfind('"')
            field = "msgid"
        elif kw == "msgstr":
            m = re.match(r"\s*msgstr\[(\d+)\]", line)
            if m:
                key = "msgstr" + str(int(m.group(1)))
                cur[key] = _qval(line)
                field = key
            else:
                cur["msgstr"] = _qval(line)
                field = "msgstr"
        elif line.lstrip().startswith('"') and field:
            parts.setdefault(field, [cur[field] or ""]).append(_qval(line))
    flush()
    return entries


def _fields(entry: dict) -> tuple:
    """What _emit writes for an entry: equal fields mean the entry is unchanged."""
    return tuple(
        sorted(
            (k, tuple(v) if isinstance(v, list) else v)
            for k, v in entry.items()
            if not k.startswith("_") and k != "msgid_ok"
        )
    )


def _emit(entry: dict) -> list[str]:
    lines = list(entry["comments"])
    if not entry.get("has_kw", True):
        # A comment-only block (an obsolete `#~` entry, a trailing comment): emitting
        # msgid ""/msgstr "" here would invent a second header.
        return lines
    if entry["msgctxt"] is not None:
        lines.append(f'msgctxt "{_escape(entry["msgctxt"])}"')
    lines.append(f'msgid "{_escape(entry["msgid"])}"')
    if entry["msgid_plural"] is not None:
        lines.append(f'msgid_plural "{_escape(entry["msgid_plural"])}"')
        indices = sorted(int(k[6:]) for k in entry if k.startswith("msgstr") and k[6:].isdigit())
        for i in indices or [0, 1]:
            lines.append(f'msgstr[{i}] "{_escape(entry.get(f"msgstr{i}") or "")}"')
    else:
        lines.append(f'msgstr "{_escape(entry.get("msgstr") or "")}"')
    return lines


_CONTENT_TYPE_RE = re.compile(r"(?m)^Content-Type:[^\n]*$")
_CHARSET_RE = re.compile(r"(?i)(\bcharset\s*=\s*)([^;\s]*)")


def _new_header() -> dict:
    """A gettext header entry for a catalog that has none (decision 9)."""
    return {
        "comments": [],
        "msgctxt": None,
        "msgid": "",
        "msgid_plural": None,
        "msgstr": (
            "MIME-Version: 1.0\n"
            "Content-Type: text/plain; charset=UTF-8\n"
            "Content-Transfer-Encoding: 8bit\n"
        ),
        "has_kw": True,
        "msgid_ok": True,
    }


def _utf8_charset(msgstr: str) -> str:
    """Decision 9: the localized catalog is written as UTF-8, so its header says so. A
    header with no Content-Type gets one; one naming another charset (or the template
    placeholder `CHARSET`) is set to UTF-8."""
    m = _CONTENT_TYPE_RE.search(msgstr)
    if m is None:
        return "Content-Type: text/plain; charset=UTF-8\n" + msgstr
    line = m.group(0)
    if _CHARSET_RE.search(line):
        new = _CHARSET_RE.sub(lambda c: c.group(1) + "UTF-8", line, count=1)
    else:
        new = line.rstrip().rstrip(";") + "; charset=UTF-8"
    return msgstr[: m.start()] + new + msgstr[m.end() :]


def _rule_key(field: str) -> tuple[str, str]:
    """(nplurals, plural expression) of a Plural-Forms line; spacing, outer parens ignored."""
    body = re.sub(r"\s+", "", field.split(":", 1)[-1])
    parts = dict(p.split("=", 1) for p in body.split(";") if "=" in p)
    expr = parts.get("plural", "")
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        for i, ch in enumerate(expr):
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if depth == 0 and i < len(expr) - 1:
                break
        else:
            expr = expr[1:-1]
            continue
        break
    return parts.get("nplurals", ""), expr


def _is_header(entry: dict) -> bool:
    return entry.get("has_kw", True) and entry["msgid"] == "" and entry["msgctxt"] is None


def _render(entries: list[dict], source: bytes | None = None, *, utf8: bool = False) -> bytes:
    """The .po text for `entries`, with exactly one header entry at most.

    Malformed input can parse into more than one msgid ""/no-msgctxt entry: a keyword
    block with no msgid line (an orphan `msgstr "stray"`) or an unterminated `msgid
    "hello`. Emitting each as its own `msgid ""` gives msgfmt a duplicate definition, so
    only the real header is kept; the others keep their comment lines and lose their
    keywords, as a comment-only block does. The real header is picked by content, so a
    malformed block never wins over it: the first entry with a well-formed msgid line
    whose msgstr holds header fields (`Content-Type: ...`), else the first with a
    well-formed msgid line, else the first."""
    heads = [e for e in entries if _is_header(e)]
    good = [e for e in heads if e.get("msgid_ok", True)]
    keep = next(
        (e for e in good if _HEADER_FIELD_RE.search(e.get("msgstr") or "")),
        good[0] if good else (heads[0] if heads else None),
    )
    for e in heads:
        if e is not keep:
            e["has_kw"] = False
    if source is not None:
        return _render_over(entries, source, utf8=utf8)
    blocks = ["\n".join(_emit(e)) for e in entries]
    return encode_text_lossless("\n\n".join(b for b in blocks if b) + "\n")


_KW_LINE_RE = re.compile(r'^(msgctxt|msgid_plural|msgid|msgstr(?:\[\d+\])?) "(.*)"$')


def _multiline(emitted: list[str]) -> list[str]:
    """gettext's wrapped style for a value with line breaks inside it (a header, a
    multi-line message): `msgstr ""` then one quoted line per `\\n`-ended piece."""
    out: list[str] = []
    for line in emitted:
        m = _KW_LINE_RE.match(line)
        value = _qval(line) if m else ""
        if not m or "\n" not in value[:-1]:
            out.append(line)
            continue
        out.append(f'{m.group(1)} ""')
        out.extend(f'"{_escape(p)}"' for p in re.findall(r"[^\n]*\n|[^\n]+$", value))
    return out


def _render_over(entries: list[dict], source: bytes, *, utf8: bool = False) -> bytes:
    """B-bytes: `entries` (parsed from `source`) written over the source's own lines. An
    entry nothing changed keeps its original lines (line wrapping, quoting, spacing);
    blank lines between entries stay as written; a changed entry is emitted. The file
    keeps its BOM, line endings and encoding (a declared charset goes back with a legacy
    code page when every character fits, else the file is UTF-8 and says so).

    `utf8`: the localized export, whose header always declares UTF-8 (decision 9,
    B-intake): its bytes are UTF-8 to match (line endings and a UTF-8 BOM still kept)."""
    lines = (
        decode_text_lossless(source, sig=True, declared=True)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .split("\n")
    )
    out: list[str] = []
    pos = 0
    for e in entries:
        span = e.get("_span")
        if span is None or span[0] < pos:
            out.extend([*_multiline(_emit(e)), ""])  # an entry the build added (a header)
            continue
        raw = lines[span[0] : span[1] + 1]
        if _fields(e) == e.get("_was"):
            block = raw
        elif any(r.lstrip().startswith('"') for r in raw):
            block = _multiline(_emit(e))  # the entry was written in gettext's wrapped style
        else:
            block = _emit(e)
        if block:  # a dropped block (a demoted malformed header) takes its gap with it
            out.extend(lines[pos : span[0]])
            out.extend(block)
        pos = span[1] + 1
    out.extend(lines[pos:])
    env = text_envelope(source, declared=True)
    if utf8:
        env = TextEnvelope("utf-8", env.bom if env.codec == "utf-8" else b"", env.newline)
    return encode_like("\n".join(out), env)


def _edge_newlines(msgid: str, text: str) -> str:
    """#712: gettext's own rule (`msgfmt -c`): a msgstr begins / ends with a newline exactly
    when its msgid does, for every msgstr[n] of a plural entry too. One translation the engine
    ended with a line break (nl `Frisian` -> `Fries` + newline) made msgfmt refuse the WHOLE
    catalog. Written translations follow the msgid; an empty text stays empty."""
    if not text:
        return text
    if msgid.startswith("\n") and not text.startswith("\n"):
        text = "\n" + text
    elif not msgid.startswith("\n") and text.startswith("\n"):
        text = text.lstrip("\n")
    if msgid.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    elif not msgid.endswith("\n") and text.endswith("\n"):
        text = text.rstrip("\n")
    return text


# #739: compact gettext expressions (C operators only) tried, in order, before the long
# one babel derives from a CLDR rule; one is used only when it gives the same slot as the
# CLDR rule for every probed count.
_COMPACT_TWO_FORM = (
    "(n != 1)",
    "(n > 1)",
    "(n % 10 == 1 && n % 100 != 11 ? 0 : 1)",
    "(n == 1 || n == 2 || n == 3 || (n % 10 != 4 && n % 10 != 6 && n % 10 != 9) ? 0 : 1)",
)
_PLURAL_PROBES = (*range(0, 2001), 10000, 100000, 1000000, 10000000)


def _target_plural(locale):
    """#739: the gettext rule for a parsed babel Locale: babel's legacy table when it
    lists the language, else the locale's CLDR plural rule as gettext (25 languages the
    legacy table lacks: id, ms, ta, ur, sw, fil, mk ...). A one-form CLDR rule (only
    `other`: id, ms, my) is `nplurals=1; plural=0`, as ja in the table.

    ValueError when the CLDR rule has no gettext form: ht, lld, scn and vec test the
    compact-exponent operand `e`, which msgfmt does not know; the caller then keeps the
    file's declarations, as for an unknown locale."""
    import gettext

    from babel.messages.plurals import PLURALS, _PluralTuple, get_plural
    from babel.plural import to_gettext

    if str(locale) in PLURALS or locale.language in PLURALS:
        return get_plural(locale)
    # "nplurals=N; plural=EXPR;" (the expression holds `?:`, so no _rule_key here)
    head, _, expr = to_gettext(locale.plural_form).partition("; plural=")
    num, expr = int(head.split("=", 1)[1]), expr.rstrip().rstrip(";")
    if num == 1:
        return _PluralTuple((1, "0"))
    cldr = gettext.c2py(expr)  # ValueError on a token msgfmt does not know (`e`)
    want = [cldr(n) for n in _PLURAL_PROBES]
    if num == 2:
        for compact in _COMPACT_TWO_FORM:
            if [gettext.c2py(compact)(n) for n in _PLURAL_PROBES] == want:
                return _PluralTuple((2, compact))
    return _PluralTuple((num, "(" + expr + ")"))


def _fill_plural_slots(
    entries: list[dict],
    original_entries: list[dict],
    paragraph_texts: dict,
    rule,
    keep_extra: bool,
) -> None:
    """#729 (decision 18): every msgstr[N] slot of the TARGET's rule gets a translation, by
    the plural-fill rule all plural-aware filters share (app/filters/plurals.py): the
    singular translation goes to the slot gettext picks for n = 1, the plural translation
    to every other slot (Arabic slot 0 is "zero": the singular goes to slot 1). Before,
    only msgstr[0] and msgstr[1] were written and every other slot stayed empty, so the
    program showed the English msgid_plural for most counts (Polish 5, Arabic 2 and up).

    #682: a one-form target (ja, zh, ko ...) uses msgstr[0] for every count, so it takes
    the plural translation (it keeps the count placeholder a singular like "One file"
    lacks); without one it keeps the singular's.

    A slot with no translation for it keeps the catalog's own text (`original_entries`,
    the same entries before `build` wrote msgstr[0] / msgstr[1]); slots past the two
    source forms are kept only when `keep_extra` (the file already follows the target's
    rule), else cleared. With `keep_extra`, a non-empty slot past the two source forms is
    the catalog's own text for that form and is kept. Unit numbering is the one `build`
    walks; `paragraph_texts` holds translations only (no source fallbacks)."""
    from app.filters.plurals import gettext_plural_slot, gettext_slot_forms

    forms = gettext_slot_forms(rule.num_plurals, rule.plural_expr)
    # Gate: with `keep_extra` the source's two units own the singular slot and ONE plural
    # slot (slot 1 as before; Arabic / Welsh, whose slot 1 is the singular's, the
    # catch-all); any other slot the catalog already filled is the translator's own form
    # (Arabic zero, two, few ...) and is never overwritten, only empty ones are filled.
    owned = {gettext_plural_slot(rule.num_plurals, rule.plural_expr)}
    owned |= {i for i, form in enumerate(forms) if form == "one"}
    # Align on the message entries only: a header entry the export adds (or drops) must
    # not make every slot fall back to the built text.
    entries_msgs = [e for e in entries if not _is_header(e)]
    orig_msgs = [e for e in original_entries if not _is_header(e)]
    if len(orig_msgs) != len(entries_msgs) or any(
        o["msgid"] != e["msgid"] for o, e in zip(orig_msgs, entries_msgs, strict=False)
    ):
        orig_msgs = entries_msgs
    k = 0
    for entry, orig in zip(entries_msgs, orig_msgs, strict=False):
        if not entry["msgid"]:
            continue
        singular = paragraph_texts.get(str(k))
        k += 1
        if not entry["msgid_plural"]:
            continue
        plural = paragraph_texts.get(str(k))
        k += 1
        for key in list(entry):
            if key.startswith("msgstr") and key[6:].isdigit() and int(key[6:]) >= len(forms):
                del entry[key]
        for i, form in enumerate(forms):
            slot = f"msgstr{i}"
            own = orig.get(slot) or ""
            if i >= 2 and not keep_extra:
                own = ""
            want = singular if form == "one" else plural
            if len(forms) == 1:
                want = plural or singular
            if want and not (keep_extra and i not in owned and own):
                entry[slot] = _edge_newlines(entry["msgid"], want)
            else:
                entry[slot] = own


@register
class PoFilter(Filter):
    fmt = "po"
    exts = ("pot",)
    writes_targets = True  # B-bytes: a target equal to its source is a real write

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        # #509: declared charset or Windows-1252 converted (header rewritten to UTF-8),
        # an unreadable file refused with a clear message.
        _text, enc = read_text_upload(data, sig=True, declared=True)
        # #571 gate: every escape is one replacement call; refuse a file with far more
        # escapes than any real catalog before the per-entry work starts.
        from app import config

        if _text.count("\\") > 40 * config.MAX_DOCUMENT_SEGMENTS:
            raise InputLimitError(
                "This PO file has far more escape sequences than MAX_DOCUMENT_SEGMENTS "
                f"allows ({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into smaller files."
            )
        units: list[TextUnit] = []
        idx = 0
        for entry in _parse_entries(data):
            # A whitespace-only `msgid " "` is DELIBERATELY kept (only a falsy msgid is
            # skipped). A `.strip()` skip here is NOT round-trip-safe: build() keys on a
            # RUNNING unit counter (`k`) over this same filtered entry sequence, so making
            # the skip predicate stricter would renumber every later unit for any doc
            # ingested before the change (its stored mapping still counts the whitespace
            # unit) — a silent target-misalignment on re-export. So this format relies on
            # the FULL_TEXT export's symmetric non-blank predicate (export.paragraph_texts_for,
            # honest-failure delta review 2026-08-13) as its sole defense against a blank
            # unit desyncing the deliverable, rather than a filter-side guard here.
            if _is_header(entry) or not entry["msgid"]:
                continue
            units.append(TextUnit(p_index=idx, text=entry["msgid"]))
            idx += 1
            check_segment_cap(idx)  # #571: stop before building the rest
            if entry["msgid_plural"]:
                units.append(TextUnit(p_index=idx, text=entry["msgid_plural"]))
                idx += 1
        skeleton = {
            "format": "po",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def build(
        self, original: bytes, paragraph_texts: dict, *, fallback_keys: set[str] | None = None
    ) -> bytes:
        paragraph_texts = {
            k: v for k, v in paragraph_texts.items() if k not in (fallback_keys or ())
        }
        entries = _parse_entries(original)
        k = 0
        for entry in entries:
            if _is_header(entry) or not entry["msgid"]:
                continue
            supplied = str(k) in paragraph_texts
            target_slot = "msgstr0" if entry["msgid_plural"] else "msgstr"
            if supplied:
                entry[target_slot] = _edge_newlines(entry["msgid"], paragraph_texts[str(k)])
            k += 1
            if entry["msgid_plural"]:
                supplied = supplied or str(k) in paragraph_texts
                if str(k) in paragraph_texts:
                    entry["msgstr1"] = _edge_newlines(entry["msgid"], paragraph_texts[str(k)])
                k += 1
            if supplied:
                comments = []
                for line in entry["comments"]:
                    if line.lstrip().startswith("#|"):
                        continue
                    if line.lstrip().startswith("#,"):
                        flags = [s.strip() for s in line.lstrip()[2:].split(",")]
                        flags = [s for s in flags if s and s != "fuzzy"]
                        if not flags:
                            continue
                        line = "#, " + ", ".join(flags)
                    comments.append(line)
                entry["comments"] = comments
        return _render(entries, original)

    def build_localized(
        self,
        original: bytes,
        paragraph_texts: dict,
        target_locale: str | None = None,
        *,
        fallback_keys: set[str] | None = None,
    ) -> bytes:
        """Stamp the Language header and its gettext rule together.

        Keep the stable two-source-unit mapping; every plural slot of the target's rule
        is filled (#729, `_fill_plural_slots`). A header without a Language field gets
        one (#688, as the MO filter already does), so the source Plural-Forms is not
        left over target text. A file without a header entry gets one with a UTF-8
        charset, Language and Plural-Forms, and every header declares the UTF-8 charset
        the export is written in (decision 9). Unknown locales retain the declarations
        of an existing header.
        """
        from app.filters import a11y

        out = self.build(original, paragraph_texts, fallback_keys=fallback_keys)
        tag = a11y.target_tag(target_locale)
        if not tag:
            return out
        parts = tag.split("-")
        region = next((p for p in parts[1:] if len(p) == 2 and p.isalpha()), "")
        value = parts[0] + ("_" + region if region else "")
        entries = _parse_entries(out)
        header = next((e for e in entries if _is_header(e)), None)
        created = header is None
        if created:
            # Decision 9: a catalog with no header entry gets one, so
            # the deliverable declares its charset (the export is always UTF-8) and its
            # language; Plural-Forms follows below as for any header (#739).
            header = _new_header()
            first_kw = next((i for i, e in enumerate(entries) if e.get("has_kw", True)), None)
            entries.insert(len(entries) if first_kw is None else first_kw, header)
        from babel.core import Locale, UnknownLocaleError

        try:
            # #739: every locale babel parses gets Language and a rule (CLDR when the
            # legacy gettext table lacks the language).
            rule = _target_plural(Locale.parse(value))
        except (UnknownLocaleError, ValueError):
            # Unknown locale (or a CLDR rule with no gettext form): preserve BOTH
            # declarations rather than inventing a rule. A header made just now still
            # names the target language.
            if created:
                header["msgstr"] += "Language: " + value + "\n"
                return _render(entries, out, utf8=True)
            return out
        header["msgstr"] = _utf8_charset(header.get("msgstr") or "")
        plural_field = re.compile(r"(?m)^Plural-Forms:[^\n]*$")
        declaration = "Plural-Forms: " + str(rule)
        found = _LANGUAGE_FIELD_RE.search(header["msgstr"])
        if found:
            source_language = found[0].split(":", 1)[1].strip()
            header["msgstr"] = _LANGUAGE_FIELD_RE.sub(
                lambda _m: "Language: " + value, header["msgstr"], count=1
            )
        else:
            # #688: no Language field, so the source language is unknown. Slots past the
            # two source forms are kept only when the file already declares the target's
            # own rule (it was written for that plural scheme, e.g. a Russian catalog
            # re-exported into Russian); otherwise they are cleared, as for a different
            # source language.
            declared = plural_field.search(header["msgstr"])
            same_rule = declared is not None and _rule_key(declared[0]) == _rule_key(declaration)
            source_language = value if same_rule else ""
            msgstr = header["msgstr"]
            header["msgstr"] = msgstr + ("\n" if msgstr and not msgstr.endswith("\n") else "")
            header["msgstr"] += "Language: " + value + "\n"
        if plural_field.search(header["msgstr"]):
            header["msgstr"] = plural_field.sub(lambda _m: declaration, header["msgstr"])
        else:
            header["msgstr"] = header["msgstr"].rstrip("\n") + "\n" + declaration + "\n"
        # the same mapping `build` wrote: a source-fallback unit is never a translation
        translated = {k: v for k, v in paragraph_texts.items() if k not in (fallback_keys or ())}
        keep_extra = source_language.replace("-", "_").split("_")[0] == parts[0]
        _fill_plural_slots(entries, _parse_entries(original), translated, rule, keep_extra)
        return _render(entries, out, utf8=True)
