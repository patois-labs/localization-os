"""key_files: .po parse / render

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports FORMATS, EMPTY_MODES, MODES, SORTS, PLURAL_STYLES,
JSON_META_KEY, JSON_META_OWNER, PO_META_COMMENT, PO_LOCK_COMMENT_PREFIX, _PO_LOCK_RE,
NS_MODE_QUALIFIED, SUB_KEPT_FOR_TRANSPORT, SUB_TM_LOCKED, _LOCK_STATE_VOCAB, _LOCK_STATE_FROM_VOCAB,
_lock_transport, SUB_EMPTY_AS_BASE, CHAR_OVER_LIMIT, FILTERED_OUT, NOTICE_NAME_CAP,
_NamespaceFromKey, _is_plural_object, _FORCED_NUMERIC_SEGMENT, _UNREPRESENTABLE_SEGMENT,
_is_forced_segment, _scalar, _record_origin, _note_merged_slot, _MSGSTR_SLOT, _MSGID_LINE,
_note_dropped_msgstr_slots, split_namespace, qualified_name, format_for_filename, _report_names from
app.services.key_files_po_parse_render_leaf.

Documented in: docs/architecture/string-keys.md
"""

from __future__ import annotations

import io
import os
import re

from sqlmodel import Session

from app.services import keys as keys_svc
from app.services.key_files_po_parse_render_leaf import (
    _FORCED_NUMERIC_SEGMENT,
    _LOCK_STATE_FROM_VOCAB,
    _LOCK_STATE_VOCAB,
    _MSGID_LINE,
    _MSGSTR_SLOT,
    _PO_LOCK_RE,
    _UNREPRESENTABLE_SEGMENT,
    CHAR_OVER_LIMIT,
    EMPTY_BASE,
    EMPTY_EMPTY,
    EMPTY_MODES,
    EMPTY_SKIP,
    FILTERED_OUT,
    FORMATS,
    JSON_META_KEY,
    JSON_META_OWNER,
    MODE_CLEANUP,
    MODE_MERGE,
    MODES,
    NOT_IN_TEMPLATE,
    NOTICE_NAME_CAP,
    NS_MODE_QUALIFIED,
    PLURAL_I18NEXT,
    PLURAL_OBJECT,
    PLURAL_STYLES,
    PO_LOCK_COMMENT_PREFIX,
    PO_META_COMMENT,
    SORT_AZ,
    SORT_FIRST_ADDED,
    SORTS,
    SUB_EMPTY_AS_BASE,
    SUB_KEPT_FOR_TRANSPORT,
    SUB_TM_LOCKED,
    _is_forced_segment,
    _is_plural_object,
    _lock_transport,
    _NamespaceFromKey,
    _note_dropped_msgstr_slots,
    _note_merged_slot,
    _record_origin,
    _report_names,
    _scalar,
    format_for_filename,
    qualified_name,
    split_namespace,
)
from app.services.keys import (
    CLDR_CATEGORIES,
    DEFAULT_NAMESPACE_LABEL,
    NESTED_SEP,
    KeyServiceError,
)

__all__ = [
    "CHAR_OVER_LIMIT",
    "CLDR_CATEGORIES",
    "EMPTY_BASE",
    "EMPTY_EMPTY",
    "EMPTY_MODES",
    "EMPTY_SKIP",
    "FILTERED_OUT",
    "FORMATS",
    "JSON_META_KEY",
    "JSON_META_OWNER",
    "MODES",
    "MODE_CLEANUP",
    "MODE_MERGE",
    "NESTED_SEP",
    "NOTICE_NAME_CAP",
    "NS_MODE_QUALIFIED",
    "PLURAL_I18NEXT",
    "PLURAL_OBJECT",
    "PLURAL_STYLES",
    "PO_LOCK_COMMENT_PREFIX",
    "PO_META_COMMENT",
    "SORTS",
    "SORT_AZ",
    "SORT_FIRST_ADDED",
    "SUB_EMPTY_AS_BASE",
    "SUB_KEPT_FOR_TRANSPORT",
    "SUB_TM_LOCKED",
    "_FORCED_NUMERIC_SEGMENT",
    "_LOCK_STATE_FROM_VOCAB",
    "_LOCK_STATE_VOCAB",
    "_MSGID_LINE",
    "_MSGSTR_SLOT",
    "_PO_LOCK_RE",
    "_UNREPRESENTABLE_SEGMENT",
    "_NamespaceFromKey",
    "_is_forced_segment",
    "_is_plural_object",
    "_lock_transport",
    "_note_dropped_msgstr_slots",
    "_note_merged_slot",
    "_record_origin",
    "_report_names",
    "_scalar",
    "format_for_filename",
    "qualified_name",
    "split_namespace",
]


def _lock_from_transport(lk) -> tuple[str, bool] | None:
    """(band, hard) for a transport lock dict, or None when its state is not a KNOWN
    XLIFF vocabulary value -- so a coincidental / malformed lock comment is IGNORED on
    import rather than trusted (MED-6/MED-9). The reverse of `_lock_transport`."""
    if not isinstance(lk, dict):
        return None
    band = _LOCK_STATE_FROM_VOCAB.get(lk.get("state"))
    if band is None:
        return None
    return (band, lk.get("translate") == "no")


NS_FROM_KEY = _NamespaceFromKey()


class KeyFileError(KeyServiceError):
    """A file this module refuses to guess about."""


def check_key_count(n: int) -> None:
    """#576: a key file may not hold more keys than MAX_DOCUMENT_SEGMENTS (the #571 cap).
    Called while a parser walks the file, so a hostile file stops at the cap instead of
    being read to the end."""
    from app import config

    if n > config.MAX_DOCUMENT_SEGMENTS:
        raise KeyFileError(
            f"this key file has more than {config.MAX_DOCUMENT_SEGMENTS} keys "
            "(MAX_DOCUMENT_SEGMENTS). Split it into smaller files."
        )


def check_key_records(records) -> None:
    """#576: the key-file twin of filters.base.check_unit_texts. Too many keys, or one
    value longer than MAX_SEGMENT_CHARS (a single huge string), refuses the whole file
    before anything is written."""
    from app import config

    limit = config.MAX_SEGMENT_CHARS
    for n, rec in enumerate(records, 1):
        check_key_count(n)
        vals = [rec.get("value"), *(rec.get("plural_values") or {}).values()]
        for v in vals:
            if isinstance(v, str) and len(v) > limit:
                raise KeyFileError(
                    f"the key {rec.get('name')!r} has a value longer than {limit} characters "
                    "(MAX_SEGMENT_CHARS). Shorten it or remove it from the file."
                )


class KeyCollisionError(KeyFileError):
    """Two paths in ONE file flatten to the same key name. Refused loudly, naming both
    -- silently merging them is how a tool loses a translation without telling anyone.

    `first`/`second` are DESCRIPTIONS of the two sides, and on the export side they
    name the real cause (review B2): the message used to talk about contexts even when
    the actual clash was two NAMESPACES sharing a key name, sending the reader looking
    for a disambiguator that was not the problem."""

    def __init__(self, name: str, first: str, second: str):
        self.name, self.first, self.second = name, first, second
        super().__init__(
            f"two entries collide on the key name {name!r} ({first} and {second}). Rename one, or import "  # noqa: E501
            "them as separate namespaces -- they will NOT be merged."
        )


def _take_json_meta(body, meta) -> None:
    """Strip OUR `$meta` marker out of a parsed body, into `meta` (see the marker block).

    Only a `$meta` object carrying the `loc_studio` envelope is ours; anything else
    named `$meta` is somebody's content and is parsed as an ordinary key, untouched."""
    if not isinstance(body, dict):
        return
    found = body.get(JSON_META_KEY)
    if not isinstance(found, dict) or not isinstance(found.get(JSON_META_OWNER), dict):
        return
    body.pop(JSON_META_KEY)
    if meta is not None:
        meta.update(found[JSON_META_OWNER])


# --- .po parse / render ------------------------------------------------------
def parse_po_keys(
    data, *, locale: str = "", notes: list | None = None, meta: dict | None = None
) -> list[dict]:
    """Parse a gettext .po/.pot into key records.

    `msgid` -> key name, `msgctxt` -> context, `#.` extracted comments -> description,
    `msgid_plural` + `msgstr[N]` -> plural forms, mapped index->CLDR category using the
    FILE'S OWN `Plural-Forms:` expression (falling back to Babel's table for the
    locale). Storing the CLDR names rather than the indices is what lets the same key
    export to JSON, i18next or Android later.

    A `#, fuzzy` entry is carried as `rec["fuzzy"]` (review B9): gettext's fuzzy flag
    means "suggested, NOT reviewed", which is exactly our needs-review state. Importing
    it as an ordinary value shipped an unreviewed string as final.

    An msgstr INDEX the file's own header has no slot for is dropped -- there is
    nowhere to put it -- but the drop is REPORTED through `notes` rather than being
    silent (review B7)."""
    from babel.messages.pofile import read_po

    raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
    from app import config

    # #576: a cheap C-speed count BEFORE babel builds a catalog (an object per entry): far
    # more entries than the cap is refused without parsing. Every entry has one `msgid `.
    if bytes(raw).count(b"msgid ") > 2 * config.MAX_DOCUMENT_SEGMENTS:
        raise KeyFileError(
            f"this key file has far more entries than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )
    try:
        catalog = read_po(io.BytesIO(bytes(raw)), locale=None)
    except Exception as exc:
        raise KeyFileError(f"this file is not a valid gettext catalog: {exc}") from exc
    if meta is not None and PO_META_COMMENT in (getattr(catalog, "header_comment", "") or ""):
        meta["namespaces"] = NS_MODE_QUALIFIED
    cats = keys_svc.gettext_categories_for(
        locale,
        getattr(catalog, "plural_expr", "") or "",
        int(getattr(catalog, "num_plurals", 0) or 0),
    )
    out: list[dict] = []
    for msg in catalog:
        if not msg.id:  # the header entry
            continue
        plural = isinstance(msg.id, (tuple, list))
        name = msg.id[0] if plural else msg.id
        # TM lock (§8, D-1): strip our `#. loc-studio-lock[form]: ...` transport comments
        # OUT of the description into rec["lock"] = {slot: lockdict}. VALIDATED both ways
        # (MED-9): a line is only treated as a lock when it matches the exact grammar AND
        # its state is a KNOWN XLIFF vocab value -- a user description that coincidentally
        # starts with the prefix but is not a real lock stays a description. Metadata only:
        # it never writes a value (K1).
        _desc_lines: list[str] = []
        _lock: dict[str, dict] = {}
        for _c in msg.auto_comments or []:
            _m = _PO_LOCK_RE.match(_c)
            _lk = (
                {"state": _m.group("state"), **({"translate": "no"} if _m.group("tr") else {})}
                if _m
                else None
            )
            if _lk is not None and _lock_from_transport(_lk) is not None:
                _lock[_m.group("form") or ""] = _lk
            else:
                _desc_lines.append(_c)
        rec = {
            "name": name,
            "context": msg.context or "",
            "description": "\n".join(_desc_lines),
            "is_plural": bool(plural),
            "fuzzy": "fuzzy" in set(getattr(msg, "flags", None) or ()),
        }
        if _lock:
            rec["lock"] = _lock
        if plural:
            strings = msg.string if isinstance(msg.string, (tuple, list)) else (msg.string,)
            forms: dict[str, str] = {}
            for idx, value in enumerate(strings):
                if idx >= len(cats):
                    continue
                cat = cats[idx]
                if cat in forms:
                    # TWO gettext slots, ONE CLDR category. Structural for the 14
                    # catalog locales with a single CLDR category and `nplurals=2`
                    # (id, ms, jv, ...): no index map can be injective, so one msgstr
                    # cannot survive. It must not survive SILENTLY (re-review R1/R8) --
                    # `setdefault` alone made a lost translation read as a clean import.
                    _note_merged_slot(notes, name, idx, cat, forms[cat], value or "")
                    continue
                forms[cat] = value or ""
            # NO `forms.setdefault("other", "")` here (review B3). ru's CLDR `other` is
            # fraction-only and has NO gettext slot, so a perfectly legitimate ru
            # catalog simply cannot carry it -- inventing an empty one made a .po round
            # trip write "" over the stored `other` translation and report `updated: 1`.
            # A category the file did not carry is silence, not an empty value.
            rec["plural_values"] = forms
            rec["plural_source"] = msg.id[1] if len(msg.id) > 1 else ""
        else:
            rec["value"] = msg.string or ""
        out.append(rec)
        check_key_count(len(out))
    _note_dropped_msgstr_slots(bytes(raw), cats, notes)
    return out


def namespace_for_filename(filename: str) -> str:
    """The default namespace an import lands in: the file's stem (`common.json` ->
    `common`), so uploading two files that both define `error.generic` produces two
    keys instead of Lokalise's silent merge.

    A stem of `__default__` means the DEFAULT namespace, not a namespace literally
    named `__default__` (re-review R3) -- that is the round trip for a qualified
    export's default section, and the label is reserved everywhere else. Any other
    unusable stem (one containing `::`) is refused here rather than at the first
    `create_key`, so the message names the FILE, which is the thing the user can
    rename."""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    if stem == DEFAULT_NAMESPACE_LABEL:
        return ""
    try:
        return keys_svc.validate_namespace(stem)
    except KeyServiceError as exc:
        raise KeyFileError(
            f"{stem!r} cannot be used as a namespace, so this file's name cannot supply one: "
            f"{exc} Import it with an explicit namespace, or rename the file."
        ) from exc


def substituted_keys(report) -> list[str]:
    """Keys whose exported value is the SOURCE text, not the translation."""
    from app.services import export as export_svc

    return _report_names(report, export_svc.SUB_AUTOMATIC)


def kept_damaged_keys(report) -> list[str]:
    """Keys a TRANSPORT export carried out with their damaged markup intact."""
    return _report_names(report, SUB_KEPT_FOR_TRANSPORT)


def tm_locked_keys(report) -> list[str]:
    """Keys a TRANSPORT export carried a TM-lock representation for (§8, D-1)."""
    return _report_names(report, SUB_TM_LOCKED)


def empty_as_base_keys(report) -> list[str]:
    """Keys whose value was WITHHELD for review and whose slot `empty_as=base` filled
    with the source text (fix-round K12)."""
    return _report_names(report, SUB_EMPTY_AS_BASE)


def over_char_limit_keys(report) -> list[str]:
    """Keys whose SHIPPED value exceeds its character limit (K8, spec §7). The value went
    out unchanged; the operator is told so they can act, not so anything was withheld."""
    return _report_names(report, CHAR_OVER_LIMIT)


def filtered_out_keys(report) -> list[str]:
    """Keys the `status=` export filter DROPPED whole (K9, live-QA F3). Named so a
    smaller-than-expected (or empty) file is legible rather than mistaken for an empty
    project."""
    return _report_names(report, FILTERED_OUT)


def not_in_template_keys(report) -> list[str]:
    """Keys a bridged resource export (Batch 9) could not place: the template file has
    no entry (or no plural form) for them, so the value did not ship."""
    return _report_names(report, NOT_IN_TEMPLATE)


_UNSAFE_IN_NAME = re.compile(r"[^\x20-\x7E]|[,\"]")


#: Everything a key name may NOT contribute to a header value or an ASCII byte
#: channel: anything outside printable US-ASCII, plus the two characters this
#: vocabulary itself uses (`,` joins names, `"` quotes them elsewhere).
def ascii_name(name: str) -> str:
    """A key name reduced to a HEADER-SAFE subset. Key names are user data and can
    carry any codepoint, so this is a security boundary, not a cosmetic one.

    IT IS NOT ENOUGH TO DROP NON-ASCII (fix-round F1, HIGH). CR, LF, NUL and TAB are
    all ASCII and survived `encode("ascii", "replace")` untouched, so a key named
    `a\\r\\nX-Injected: yes` -- creatable by anyone with write access, from `keys add
    --name`, the grid's add form or a JSON import -- put a bare CRLF into
    `X-Export-Substituted-Keys`. uvicorn's httptools writer concatenates header bytes
    with no validation at all (`content.extend([name, b": ", value, b"\\r\\n"])`), so
    that is an attacker-controlled HEADER AND BODY injected into a response from the
    app's own origin; on the h11 fallback it is a `LocalProtocolError` and a 500 on a
    download that otherwise worked. An ALLOW-list of printable US-ASCII is the only
    shape that holds -- a deny-list of "the control characters we thought of" is the
    same bug waiting for the next one.

    `,` and `"` go too, so a key genuinely named `a,b` cannot masquerade as two keys
    in the joined header the `--remote` arm parses back (fix-round F8). The name is
    already approximate for any non-ASCII key; `?` for a delimiter is the same trade,
    and the COUNT header -- which is what a CI job gates on -- is exact regardless."""
    return _UNSAFE_IN_NAME.sub("?", str(name))


def download_filename(stem: str, locale: str, ext: str) -> str:
    """The `Content-Disposition` filename for an export download, built from
    HEADER-SAFE pieces only.

    FOUND WHILE FIXING F1, SAME CELL, WIDER BLAST RADIUS. Both download routes built
    this straight out of the `namespace` and `locale` QUERY PARAMETERS. An unknown
    namespace is not an error -- it simply selects no keys -- so
    `?namespace=nav%0d%0aX-Injected:+yes` produced
    `attachment; filename="nav\\r\\nX-Injected: yes.ru-RU.json"` and split the response
    on the same unvalidated uvicorn writer F1 rides. It needs no write access at all,
    only the ability to READ a key project, and `?namespace=nav";+filename="evil.exe`
    additionally broke out of the quoted filename.

    `ascii_name` is the one sanitizer (its deny-list already covers `"`); `\\` goes too,
    because a trailing backslash escapes the closing quote of a quoted-string. Both
    pieces are length-capped so a 4,000-character namespace cannot blow the header
    budget from the other side."""

    def clean(part: str, cap: int) -> str:
        return (ascii_name(part).replace("\\", "?")[:cap]).strip() or "export"

    return f"{clean(stem, 80)}.{clean(locale, 40)}.{clean(ext, 24)}"


def export_notices(report, *, cap: int = NOTICE_NAME_CAP) -> list[str]:
    """THE ONE rendering of an export's tag-guard outcome -- CLI human output, the
    `--json` payload's text, the web notice, the audit detail's companion.

    Empty for a clean export, so nothing changes on the overwhelmingly common path.
    ASCII by construction (`ascii_name`), because two of the three surfaces are byte
    channels that cannot carry anything else."""
    lines: list[str] = []
    subs = substituted_keys(report)
    kept = kept_damaged_keys(report)
    withheld = empty_as_base_keys(report)
    if subs:
        lines.append(
            "%d value(s) shipped SOURCE text because their tags were damaged: %s"  # noqa: UP031
            % (len(subs), _name_list(subs, cap))
        )
    if kept:
        lines.append(
            "%d value(s) carry damaged tags and were kept VERBATIM (this is a "  # noqa: UP031
            "transport file, so it re-imports unchanged): %s" % (len(kept), _name_list(kept, cap))
        )
    if withheld:
        lines.append(
            "%d value(s) were WITHHELD for review and empty-as=base shipped their "  # noqa: UP031
            "source text instead: %s" % (len(withheld), _name_list(withheld, cap))
        )
    over = over_char_limit_keys(report)
    if over:
        lines.append(
            "%d value(s) shipped but exceed their character limit: %s"  # noqa: UP031
            % (len(over), _name_list(over, cap))
        )
    filtered = filtered_out_keys(report)
    if filtered:
        lines.append(
            "%d key(s) were FILTERED OUT of this export by the status filter "  # noqa: UP031
            "(dropped whole, not shipped): %s" % (len(filtered), _name_list(filtered, cap))
        )
    locked = tm_locked_keys(report)
    if locked:
        lines.append(
            "%d value(s) carry a TM lock, carried on this transport file so a round trip "  # noqa: UP031
            "preserves it (a build file never carries it): %s"
            % (len(locked), _name_list(locked, cap))
        )
    missing = not_in_template_keys(report)
    if missing:
        lines.append(
            "%d key(s) have no place in this resource file's template and did not "  # noqa: UP031
            "ship: %s" % (len(missing), _name_list(missing, cap))
        )
    return lines


HDR_SUBSTITUTED = "X-Export-Substituted"


HDR_SUBSTITUTED_DETAIL = "X-Export-Substituted-Detail"


HDR_SUBSTITUTED_KEYS = "X-Export-Substituted-Keys"


HDR_KEPT = "X-Export-Kept-Damaged"


HDR_KEPT_KEYS = "X-Export-Kept-Damaged-Keys"


HDR_WITHHELD = "X-Export-Withheld-Filled"


HDR_WITHHELD_KEYS = "X-Export-Withheld-Filled-Keys"


HDR_OVER_LIMIT = "X-Export-Over-Char-Limit"


HDR_OVER_LIMIT_KEYS = "X-Export-Over-Char-Limit-Keys"


HDR_FILTERED = "X-Export-Filtered-Out"


HDR_FILTERED_KEYS = "X-Export-Filtered-Out-Keys"


HDR_NOT_IN_TEMPLATE = "X-Export-Not-In-Template"


HDR_NOT_IN_TEMPLATE_KEYS = "X-Export-Not-In-Template-Keys"


HEADER_NAME_CAP = 40


HEADER_VALUE_BUDGET = 900


_MORE = re.compile(r"^\+\d+ more$")


_DETAIL_OVERHEAD = 32


#: Header names for a DOWNLOAD, which has no other channel: a browser gets bytes and
#: a status line. `X-Export-Substituted[-Detail]` are the SAME two the document export
#: has emitted since #42 -- one vocabulary, so an operator who learned it on documents
#: reads a key export without being taught again.
#: K8: the count + names of keys whose shipped value is over its character limit. Same
#: shape as the withheld pair -- a count a CI job can gate on and a name list -- so the
#: `--remote` arm rebuilds the notice from them (report_from_headers).
#: K9 (live-QA F3): the count + names of keys the status filter DROPPED. Same shape as
#: every pair above so the `--remote` arm rebuilds the notice from them
#: (report_from_headers) and a CI job can gate on the count.
#: Header values are bounded BY BYTES, not by name count (fix-round F2). A count cap
#: alone bounds nothing a proxy cares about: key names are user data of unbounded
#: length, and 40 four-thousand-character names is a ~160 KB header against nginx's and
#: Fly's ~8 KB default -- a download that fails with a protocol error nowhere near its
#: cause. The count cap stays as a cheap first cut; the BYTE budget is the real one.
#: What a truncated name list ends with. It cannot be mistaken for a key name on the
#: way back: `report_from_headers` knows this exact shape and drops it.
#: `substitution_detail` spends this much per entry on top of the name itself
#: (`kind=automatic unit=key index=` and a separator), so budgeting the NAME list by
#: this stride keeps the DETAIL header CLOSE TO the same budget as the KEYS header --
#: not strictly inside it. Corrected in the final round: this stride counts the
#: per-entry text only and not `substitution_detail`'s one-off `substituted=N ` prefix,
#: so a worst-case DETAIL value measured 912 bytes against a 900-byte budget. Twelve
#: bytes against an 8 KB proxy limit is immaterial and the constant is deliberately
#: left alone; the claim of a hard bound was not, and is now an approximate one. The
#: HARD bound that matters (F2's ~160 KB header) is enforced by the loop below.
def _fit_names(names: list[str], *, overhead: int = 0) -> tuple[list[str], int]:
    """The prefix of `names` (already ASCII-sanitized, so len == bytes) that fits
    `HEADER_VALUE_BUDGET`, plus how many were dropped. At least one name always
    survives, truncated if it has to be: "the header said nothing at all" is a worse
    answer than "the header named one key, shortened"."""
    out: list[str] = []
    used = 0
    for name in names:
        cost = len(name) + overhead + (1 if out else 0)
        if used + cost > HEADER_VALUE_BUDGET:
            break
        out.append(name)
        used += cost
    if not out and names:
        out = [names[0][: max(HEADER_VALUE_BUDGET - overhead, 1)]]
    return out, len(names) - len(out)


def _header_list(names: list[str]) -> str:
    """The joined, sanitized, byte-budgeted value of an `...-Keys` header."""
    shown, rest = _fit_names(
        [ascii_name(n) for n in names[:HEADER_NAME_CAP]], overhead=_DETAIL_OVERHEAD
    )
    rest += max(len(names) - HEADER_NAME_CAP, 0)
    return ",".join(shown) + (",+%d more" % rest if rest > 0 else "")  # noqa: UP031


def export_headers(report) -> dict:
    """The download headers carrying an export's tag-guard facts. `{}` for a clean
    export, so an untouched download's headers stay byte-identical.

    Every name that reaches a value here goes through `ascii_name` -- see its docstring
    for why that is a security boundary and not a formatting nicety (fix-round F1)."""
    from app.services import export as export_svc

    subs, kept = substituted_keys(report), kept_damaged_keys(report)
    withheld = empty_as_base_keys(report)
    out: dict = {}
    if subs:
        out[HDR_SUBSTITUTED] = str(len(subs))
        shown, _rest = _fit_names(
            [ascii_name(n) for n in subs[:HEADER_NAME_CAP]], overhead=_DETAIL_OVERHEAD
        )
        out[HDR_SUBSTITUTED_DETAIL] = export_svc.substitution_detail(
            [{"unit": "key", "kind": export_svc.SUB_AUTOMATIC, "index": n} for n in shown]
        )
        out[HDR_SUBSTITUTED_KEYS] = _header_list(subs)
    if kept:
        out[HDR_KEPT] = str(len(kept))
        out[HDR_KEPT_KEYS] = _header_list(kept)
    if withheld:
        out[HDR_WITHHELD] = str(len(withheld))
        out[HDR_WITHHELD_KEYS] = _header_list(withheld)
    over = over_char_limit_keys(report)
    if over:
        out[HDR_OVER_LIMIT] = str(len(over))
        out[HDR_OVER_LIMIT_KEYS] = _header_list(over)
    filtered = filtered_out_keys(report)
    if filtered:
        out[HDR_FILTERED] = str(len(filtered))
        out[HDR_FILTERED_KEYS] = _header_list(filtered)
    missing = not_in_template_keys(report)
    if missing:
        out[HDR_NOT_IN_TEMPLATE] = str(len(missing))
        out[HDR_NOT_IN_TEMPLATE_KEYS] = _header_list(missing)
    return out


def audit_detail(report) -> str:
    """The `keys.export` audit row's detail -- BOTH guard outcomes, not just one
    (fix-round F9). The routes filtered to `SUB_AUTOMATIC` before writing this, so a
    TRANSPORT export that carried damaged markup out reached the headers, the cookie
    and the CLI and left the audit trail saying nothing at all -- while the spec lists
    the audit among the four surfaces K2 has to reach.

    "" for a clean export, so an untouched export's audit row stays byte-identical."""
    from app.services import export as export_svc

    parts = [
        export_svc.substitution_detail(
            [
                e
                for e in (report or [])
                if isinstance(e, dict) and e.get("kind") == export_svc.SUB_AUTOMATIC
            ]
        )
    ]
    for names, label in (
        (kept_damaged_keys(report), "kept_damaged"),
        (empty_as_base_keys(report), "withheld_filled"),
        (over_char_limit_keys(report), "over_char_limit"),
        (filtered_out_keys(report), "filtered_out"),
        (not_in_template_keys(report), "not_in_template"),
    ):
        if names:
            parts.append("%s=%d keys=%s" % (label, len(names), _name_list(names, NOTICE_NAME_CAP)))  # noqa: UP031
    return " ".join(p for p in parts if p)


def report_from_headers(headers) -> list:
    """The inverse, for the `--remote` CLI arm: rebuild a `report` from a download's
    headers so the remote export prints what the local one prints. Parity here is not
    cosmetic -- `--remote` is how a CI job runs this verb."""
    from app.services import export as export_svc

    out: list = []
    for header, kind in (
        (HDR_SUBSTITUTED_KEYS, export_svc.SUB_AUTOMATIC),
        (HDR_KEPT_KEYS, SUB_KEPT_FOR_TRANSPORT),
        (HDR_WITHHELD_KEYS, SUB_EMPTY_AS_BASE),
        (HDR_OVER_LIMIT_KEYS, CHAR_OVER_LIMIT),
        (HDR_FILTERED_KEYS, FILTERED_OUT),
        (HDR_NOT_IN_TEMPLATE_KEYS, NOT_IN_TEMPLATE),
    ):
        raw = ""
        try:
            raw = headers.get(header) or headers.get(header.lower()) or ""
        except AttributeError:
            raw = ""
        for name in [n.strip() for n in str(raw).split(",") if n.strip()]:
            if _MORE.match(name):  # the truncation marker, not a key
                continue
            out.append({"unit": "key", "index": name, "kind": kind})
    return out


def _name_list(names, cap: int) -> str:
    shown = [ascii_name(n) for n in names[: max(cap, 1)]]
    rest = len(names) - len(shown)
    return ", ".join(shown) + (" and %d more" % rest if rest > 0 else "")  # noqa: UP031


_CTRL_IN_NAME = re.compile(r"[\x00-\x1f\x7f]")


IMPORT_NOTICE_BUDGET = 620


#: Control characters are the only thing a key name may NOT carry into a notice. The
#: notice is percent-encoded into a cookie and HTML-escaped on the way out, so unlike
#: `ascii_name`'s header path this is a legibility rule, not a security boundary --
#: which is why a Cyrillic or CJK key name keeps its own spelling here.
#: Characters `import_notices` may spend on NAMES. The web grid's one-shot cookie is
#: capped at 900; this leaves room for the label, the explanatory sentence and the
#: second line without the cap ever having to cut anything.
def fit_name_list(names, budget: int) -> str:
    """`a, b, c, +37 more` -- as many names as fit in `budget` CHARACTERS, never one
    cut in half, and ALWAYS with the overflow announced (final round, blocker 2).

    The failure this replaces: the grid's notice was a sentence per key hard-sliced at
    900 characters, so 40 skipped keys were named 7 of and the string stopped
    mid-word -- a reader had no way to know the list was partial, let alone by how
    much. `+N more` is a claim about the remainder, so it is computed against the names
    ACTUALLY shown rather than against a fixed cap."""
    clean = [_CTRL_IN_NAME.sub("?", str(n)) for n in names]
    shown: list[str] = []
    used = 0
    for name in clean:
        rest_if_stop_here = len(clean) - len(shown) - 1
        marker = len(", +%d more" % rest_if_stop_here) if rest_if_stop_here > 0 else 0  # noqa: UP031
        cost = (2 if shown else 0) + len(name)
        if used + cost + marker > max(budget, 1):
            break
        shown.append(name)
        used += cost
    if not shown and clean:
        # One pathological name longer than the whole budget still gets a stump, so
        # the notice never comes out empty and the count still tells the truth.
        shown = [clean[0][: max(budget - 12, 1)]]
    rest = len(clean) - len(shown)
    return ", ".join(shown) + (", +%d more" % rest if rest > 0 else "")  # noqa: UP031


def import_notices(report, *, budget: int = IMPORT_NOTICE_BUDGET) -> list[str]:
    """An import report rendered for a surface with room for a STRING, not a paragraph
    -- i.e. the web grid's one-shot notice (final round, blocker 2).

    `export_notices`' opposite number, and the same shape: names, a count, and an
    explicit overflow marker. The CLI, `--json` and /api keep the full `notes[]`
    sentences; the grid gets the names, which is the part `keys.md` promises."""
    rep = report or {}
    skipped = list(rep.get("skipped_keys") or [])
    notes = list(rep.get("notes") or [])
    lines: list[str] = []
    if skipped:
        lines.append(
            "%d value(s) were NOT written because the file carried the SOURCE text "  # noqa: UP031
            "for this locale, which is not a translation: %s."
            % (len(skipped), fit_name_list(skipped, budget))
        )
    other = len(notes) - len(skipped)
    if other > 0:
        lines.append(
            "%d other note(s) -- the command line and the --json report "  # noqa: UP031
            "carry their full text." % other
        )
    return lines


def _resolve_locale(session: Session, proj, locale: str, verb: str) -> str:
    """The project's OWN spelling of `locale`, or a refusal.

    Validity is "the source locale, or an ACTIVE target locale" -- matched on the
    normalized form, so `en_US` resolves to the project's `en-US` instead of being
    treated as a brand-new target (review B11/B18). Refusing UP FRONT is the point: an
    import used to create the entire key set and then fail every value, and an export
    to a removed locale returned stale or empty output rather than saying so.

    An EXPORT also accepts a language a connected project has STOPPED: its translations
    stay readable and exportable (2026-10-13, decision 4); an import does not."""
    from app.services import languages_catalog
    from app.services.keys_common import readable_locales

    want = (locale or "").strip()
    targets = (
        readable_locales(session, proj.id)
        if verb == "export"
        else keys_svc.active_locales(session, proj.id)
    )
    candidates = [proj.source_locale] + targets  # noqa: RUF005
    for known in candidates:
        if known == want:
            return known
    norm = languages_catalog.normalize(want)
    for known in candidates:
        if languages_catalog.normalize(known) == norm:
            return known
    raise KeyFileError(
        "{!r} is not this project's source locale ({}) or one of its active target "
        "locales ({}), so there is nothing to {} for it.".format(
            want,
            proj.source_locale,
            ", ".join(targets) or "none",
            verb,
        )
    )


def _key_label(key) -> str:
    return qualified_name(key.namespace or "", key.name)


def _note(notes: list | None, msg: str) -> None:
    if notes is not None:
        notes.append(msg)


def _note_skipped(notes: list | None, skipped: list | None, label: str, msg: str) -> None:
    """A K1(c) skip, recorded TWICE: the sentence for the surfaces that can carry a
    sentence, and the bare key label for the ones that cannot (final round, blocker 2).

    The grid gets ONE bounded string, and a sentence per key meant 40 skipped keys were
    named 7 of -- the other 33 fell off a character cap mid-word. Names are ~12
    characters where a sentence is ~150, so the same budget names them all."""
    _note(notes, msg)
    if skipped is not None:
        skipped.append(label)


def _fold(text: str) -> str:
    """The form the K1(c) source-restatement guard compares on: NFC and stripped, and
    DELIBERATELY NOT casefolded.

    NFC + strip is what fix-round F3 actually needed -- byte identity made the whole
    rule one trailing newline from bypass, and NFC additionally catches the
    decomposed-vs-composed spelling a Mac-edited file emits.

    Casefolding was added with it and is REMOVED here (final round, blocker 1): it
    makes every case-only difference from the source read as "source text", so a
    legitimate translation is refused into an empty row -- `Email`, `Internet`,
    `iPhone`, `SAVE`. The killer is not English but a regional variant:
    `casefold("ß") == "ss"`, and Swiss German differs from German ONLY by that
    substitution, so a de-DE -> de-CH project could not import its target locale AT
    ALL -- every value blocked into an empty row, and an existing wrong value not even
    correctable. A whole locale pair for a bypass NFC + strip already closes."""
    import unicodedata

    return unicodedata.normalize("NFC", text or "").strip()


def _restates_source(incoming: str, base_text: str) -> bool:
    """True when `incoming` says nothing more than this form's own base text does."""
    return bool(base_text.strip()) and _fold(incoming) == _fold(base_text)
