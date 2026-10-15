"""Email message filter (.eml, RFC 5322 + MIME).

What is translated: the Subject header and every text/plain and text/html body part that
is not an attachment (Content-Disposition: attachment), in document order: Subject first,
then the leaf parts as they appear in the file (both halves of a multipart/alternative).
HTML parts go through `html_filter.HtmlFilter` (block units, inline tags as tokens, alt
text). Plain parts are split into paragraphs: a format=flowed part (RFC 3676) is unwrapped
into its logical lines; a fixed part joins hard-wrapped lines (a line of 60+ characters
followed by a non-indented, non-list line) and re-wraps a translation at the paragraph's
original width. Quoted lines (">"), the signature separator and blank lines are kept.

Fidelity: the stdlib `email` package is used only to READ header blocks (RFC 2047/2231
decoding); it never re-serializes the message. A small MIME walker records the byte range
of every header block and body, and build() splices only the changed ranges, so headers,
attachments, inline images, Content-IDs, boundaries, the preamble and epilogue, and every
untouched part come back byte for byte. A changed body is re-encoded in its original
charset and Content-Transfer-Encoding; when the translation does not fit (a charset that
cannot hold it, 7bit with non-ASCII, an 8bit line over 998 bytes) the part falls back to
UTF-8 and/or quoted-printable and its Content-Type / Content-Transfer-Encoding headers are
rewritten to say so (the headers are the record). A changed Subject is re-encoded as an
RFC 2047 encoded word (original charset when it can hold the text, else UTF-8).

Security: nothing is fetched; links, cid: references and message/external-body are never
followed. S/MIME and PGP signed or encrypted messages are refused (a translation would
break the signature). Caps: part count, nesting depth, header block size, plus the shared
segment and unit caps. message/rfc822 attachments (forwarded mail) are kept as attachments.

Outlook .msg (OLE compound file) is not handled: rewriting a stream of a different length
needs a full compound-file writer (olefile only overwrites same-size streams).

Specs: RFC 5322, RFC 2045/2046/2047/2231, RFC 3676 (format=flowed), RFC 3282
(Content-Language), RFC 1847 + RFC 8551 (S/MIME), RFC 3156 (PGP/MIME).
"""

import base64
import binascii
import codecs
import quopri
import re
import textwrap
from dataclasses import dataclass, field
from email import policy
from email.header import Header, decode_header, make_header
from email.parser import BytesHeaderParser

from app.filters import a11y
from app.filters.base import (
    _DECL_RE,
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
from app.filters.html_filter import HtmlFilter

MAX_PARTS = 500  # MIME parts (every node, containers included)
MAX_DEPTH = 16  # multipart nesting
MAX_HEADER_BLOCK = 1024 * 1024  # bytes in one header block
MAX_LINE = 998  # RFC 5322 section 2.1.1 (excluding CRLF)

_REFUSED_TYPES = {
    "multipart/signed": "signed",
    "multipart/encrypted": "encrypted",
    "application/pkcs7-mime": "S/MIME",
    "application/x-pkcs7-mime": "S/MIME",
    "application/pkcs7-signature": "signed",
    "application/x-pkcs7-signature": "signed",
    "application/pgp-encrypted": "encrypted",
    "application/pgp-signature": "signed",
    "application/pkcs7-encrypted": "encrypted",
}
_PGP_INLINE = ("-----BEGIN PGP SIGNED MESSAGE-----", "-----BEGIN PGP MESSAGE-----")
_SIGNED_MSG = (
    "This email is {kind} (S/MIME or PGP). Translating it would break the signature or "
    "cannot read the encrypted text; export the message without signing or encryption "
    "and upload that instead."
)
_FIELD_START = re.compile(rb"^[!-9;-~]+[ \t]*:")
_LIST_MARK = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s")
_HTML = HtmlFilter(page_meta=False)  # #733: parts are concatenated into one document


class EmailRefusedError(UnsupportedEncodingError):
    """An .eml upload we will not translate: signed or encrypted, not an email at all, an
    unknown charset or unreadable text. The message is plain and safe to show; as an
    UnsupportedEncodingError (an InputLimitError) every surface shows it instead of a
    generic 'corrupted', and the API answers 422 (it is not over any limit)."""


class EmailLimitError(InputLimitError):
    """An .eml over a structural cap (MIME parts, nesting, header block): 413 on the API."""


# ---------------------------------------------------------------------------- MIME walk


@dataclass
class _Part:
    start: int  # header block start
    hdr_end: int  # end of the header fields (the blank line starts here)
    body_start: int
    end: int
    headers: object  # email.message.EmailMessage (headers only)
    ctype: str
    depth: int
    children: list = field(default_factory=list)


_BLANK = re.compile(rb"\n\r?\n")


def _header_end(data: bytes, start: int, end: int) -> tuple[int, int]:
    """(end of header fields, start of body) for the entity in data[start:end]. One
    C-speed search bounded by MAX_HEADER_BLOCK, never a per-line Python loop."""
    for lead in (b"\r\n", b"\n"):  # no header fields at all: the body starts at once
        if data.startswith(lead, start) and start + len(lead) <= end:
            return start, start + len(lead)
    m = _BLANK.search(data, start, min(end, start + MAX_HEADER_BLOCK + 4))
    if m is None:
        if end - start > MAX_HEADER_BLOCK:
            raise EmailLimitError("This email has a header block over 1 MB; refused.")
        return end, end
    return m.start() + 1, m.end()


def _parse_part(data: bytes, start: int, end: int, depth: int, count: list) -> _Part:
    count[0] += 1
    if count[0] > MAX_PARTS:
        raise EmailLimitError(
            f"This email has more than {MAX_PARTS} MIME parts. Split it or remove attachments."
        )
    if depth > MAX_DEPTH:
        raise EmailLimitError(f"This email nests MIME parts deeper than {MAX_DEPTH} levels.")
    hdr_end, body_start = _header_end(data, start, end)
    if hdr_end - start > MAX_HEADER_BLOCK:
        raise EmailLimitError("This email has a header block over 1 MB; refused.")
    try:
        headers = BytesHeaderParser(policy=policy.default).parsebytes(data[start:hdr_end])
        ctype = headers.get_content_type()
    except Exception as exc:  # malformed header block
        raise EmailRefusedError("This email's MIME headers could not be read.") from exc
    kind = _REFUSED_TYPES.get(ctype)
    if kind:
        raise EmailRefusedError(_SIGNED_MSG.format(kind=kind))
    part = _Part(start, hdr_end, body_start, end, headers, ctype, depth)
    if headers.get_content_maintype() == "multipart":
        try:
            boundary = headers.get_param("boundary")
        except Exception:
            boundary = None
        if isinstance(boundary, tuple):  # RFC 2231 form
            boundary = boundary[2]
        if boundary and isinstance(boundary, str) and 0 < len(boundary) <= 200:
            for s, e in _split_multipart(data, body_start, end, boundary):
                part.children.append(_parse_part(data, s, e, depth + 1, count))
    return part


def _split_multipart(data: bytes, start: int, end: int, boundary: str):
    """Byte ranges of the body parts between the delimiters (RFC 2046 section 5.1.1).
    The line break before a delimiter belongs to the delimiter, so it is not in a part."""
    try:
        b = boundary.encode("ascii")
    except UnicodeEncodeError:
        return []
    pat = re.compile(rb"(?m)^--" + re.escape(b) + rb"(--)?[ \t]*\r?$")
    ranges = []
    open_at = None
    for m in pat.finditer(data, start, end):
        if open_at is not None:
            stop = m.start()
            if stop >= 2 and data[stop - 2 : stop] == b"\r\n":
                stop -= 2
            elif stop >= 1 and data[stop - 1 : stop] == b"\n":
                stop -= 1
            ranges.append((open_at, max(open_at, stop)))
        if m.group(1):  # close delimiter
            return ranges
        nl = data.find(b"\n", m.end(), end)
        open_at = end if nl < 0 else nl + 1
    if open_at is not None:  # no close delimiter: the last part runs to the end
        ranges.append((open_at, end))
    return ranges


def _leaves(part: _Part):
    yield part
    for c in part.children:
        yield from _leaves(c)


# ------------------------------------------------------------------------ body decoding


def _cte(part: _Part) -> str:
    return str(part.headers.get("content-transfer-encoding", "7bit") or "7bit").strip().lower()


def _translatable(part: _Part) -> bool:
    if part.children or part.ctype not in ("text/plain", "text/html"):
        return False
    try:
        disp = part.headers.get_content_disposition()
    except Exception:
        disp = None
    return disp != "attachment" and _cte(part) in (
        "7bit",
        "8bit",
        "binary",
        "quoted-printable",
        "base64",
    )


def _decode_cte(raw: bytes, cte: str) -> bytes:
    if cte == "base64":
        try:
            return binascii.a2b_base64(raw)
        except (binascii.Error, ValueError) as exc:
            raise EmailRefusedError("A base64 text part of this email is damaged.") from exc
    if cte == "quoted-printable":
        return binascii.a2b_qp(raw)
    return raw


def _text_codec(label: str) -> str:
    try:
        info = codecs.lookup(label)
        "".encode(info.name)  # bytes-to-bytes codecs (base64, zlib) fail here
    except (LookupError, TypeError) as exc:
        raise EmailRefusedError(
            f"This email has a text part in an unknown charset ({label[:40]})."
        ) from exc
    return info.name


@dataclass
class _Slot:
    kind: str  # "subject" | "plain" | "html"
    part: _Part
    text: str = ""
    codec: str = ""  # codec used to decode
    declared: bool = False  # charset parameter present
    flowed: bool = False
    delsp: bool = False
    raw_field: bytes = b""  # subject: the whole field bytes
    field_range: tuple = ()  # subject: (start, end) in data
    eight_bit: bool = False  # subject: raw UTF-8 header (RFC 6532)
    charset_hint: str = ""  # subject: charset of its first encoded word


def _decode_body(data: bytes, part: _Part) -> tuple[str, str, bool]:
    raw = _decode_cte(data[part.body_start : part.end], _cte(part))
    try:
        label = part.headers.get_param("charset")
    except Exception:
        label = None
    if isinstance(label, tuple):
        label = label[2]
    if label:
        codec = _text_codec(str(label).strip())
        try:
            return raw.decode(codec), codec, True
        except UnicodeDecodeError as exc:
            if codec == "ascii":  # common mislabel: 8-bit text marked us-ascii
                for alt in ("utf-8", "cp1252"):
                    try:
                        return raw.decode(alt), alt, False
                    except UnicodeDecodeError:
                        continue
            raise EmailRefusedError(
                f"A text part of this email declares charset {label} but its bytes do not "
                "match it. Re-save the message and upload it again."
            ) from exc
    for codec in ("ascii", "utf-8", "cp1252"):  # no charset: RFC 2045 says US-ASCII
        try:
            return raw.decode(codec), codec, False
        except UnicodeDecodeError:
            continue
    raise EmailRefusedError("A text part of this email is not readable text.")


# ---------------------------------------------------------------------------- headers


def _fields(block: bytes) -> list[list]:
    """Split a header block into [name_lower, raw_bytes] fields (continuation lines kept)."""
    out: list[list] = []
    for line in block.splitlines(keepends=True):
        if line[:1] in (b" ", b"\t") and out:
            out[-1][1] += line
        else:
            name = line.split(b":", 1)[0].strip().lower().decode("latin-1")
            out.append([name, line])
    return out


def _unfold(raw: bytes) -> bytes:
    return re.sub(rb"\r?\n(?=[ \t])", b"", raw).rstrip(b"\r\n")


def _subject_slot(data: bytes, top: _Part) -> _Slot | None:
    pos = top.start
    for name, raw in _fields(data[top.start : top.hdr_end]):
        if name == "subject":
            value = _unfold(raw).split(b":", 1)[1].lstrip(b" \t")
            hint = re.search(rb"=\?([^?*]+)", value)
            eight = not value.isascii()
            try:
                text = value.decode("utf-8") if eight else value.decode("ascii")
                if "=?" in text:
                    text = str(make_header(decode_header(text)))
            except (UnicodeDecodeError, LookupError, ValueError, binascii.Error):
                return None
            return _Slot(
                "subject",
                top,
                text=text,
                raw_field=raw,
                field_range=(pos, pos + len(raw)),
                eight_bit=eight,
                charset_hint=hint.group(1).decode("ascii", "replace") if hint else "",
            )
        pos += len(raw)
    return None


def _encode_subject(slot: _Slot, new: str, eol: str) -> bytes:
    new = " ".join(new.split())  # no line breaks inside a header
    if slot.eight_bit:
        return ("Subject: " + new + eol).encode("utf-8")
    if new.isascii():
        enc = Header(new, "us-ascii", header_name="Subject").encode(linesep=eol, maxlinelen=78)
    else:
        cs = "utf-8"
        hint = slot.charset_hint.lower()
        if hint:
            try:
                new.encode(_text_codec(hint))
                cs = hint
            except (UnicodeEncodeError, EmailRefusedError):
                cs = "utf-8"
        enc = Header(new, cs, header_name="Subject").encode(linesep=eol, maxlinelen=78)
    return ("Subject: " + enc + eol).encode("ascii" if not slot.eight_bit else "utf-8")


def _edit_block(block: bytes, eol: bytes, edits: dict) -> bytes:
    """Apply header edits to one raw header block. edits keys: 'subject' (whole field
    bytes), 'cte' (str), 'charset' (str), 'lang' (str, only where already present)."""
    fields = _fields(block)
    out = []
    seen_ct = seen_cte = False
    for name, raw in fields:
        if name == "subject" and "subject" in edits:
            raw = edits["subject"]
        elif name == "content-transfer-encoding" and "cte" in edits:
            seen_cte = True
            raw = b"Content-Transfer-Encoding: " + edits["cte"].encode("ascii") + eol
        elif name == "content-type" and "charset" in edits:
            seen_ct = True
            raw = _set_charset(raw, edits["charset"], eol)
        elif name == "content-language" and edits.get("lang"):
            raw = b"Content-Language: " + edits["lang"].encode("ascii") + eol
        out.append(raw)
    if "charset" in edits and not seen_ct:
        out.append(b'Content-Type: text/plain; charset="' + edits["charset"].encode() + b'"' + eol)
    if "cte" in edits and not seen_cte:
        out.append(b"Content-Transfer-Encoding: " + edits["cte"].encode("ascii") + eol)
    return b"".join(out)


def _set_charset(raw: bytes, charset: str, eol: bytes) -> bytes:
    body = raw.rstrip(b"\r\n")
    ending = raw[len(body) :] or eol
    cs = charset.encode("ascii")
    pat = re.compile(rb'(;\s*charset\s*=\s*)("[^"]*"|[^;\s]*)', re.IGNORECASE)
    if pat.search(body):
        body = pat.sub(lambda m: m.group(1) + b'"' + cs + b'"', body, count=1)
    else:
        body = body + b'; charset="' + cs + b'"'
    return body + ending


# ------------------------------------------------------------------------- plain text


def _plain_items(text: str, flowed: bool, delsp: bool) -> list:
    """Split a text/plain body into items: str (kept verbatim) or dict units
    {"text", "raw", "lead", "trail", "width", "multi"}. parse and build share this walk."""
    check_line_budget(text)
    lines = text.splitlines(keepends=True)
    items: list = []
    if flowed:
        i = 0
        while i < len(lines):
            # gather one flowed paragraph: lines ending in SP (except "-- ") continue
            j = i
            parts = []
            while j < len(lines):
                body = lines[j].rstrip("\r\n")
                if j > i and (
                    body.startswith(">") != lines[i].startswith(">") or body in ("-- ", "--")
                ):
                    break  # a change of quote depth or the signature ends the paragraph
                content = body[1:] if body.startswith(" ") else body  # unstuff
                parts.append(content)
                j += 1
                if not (body.endswith(" ") and body != "-- ") or body.startswith(">"):
                    break
            raw = "".join(lines[i:j])
            first = lines[i]
            if first.startswith(">") or first.rstrip("\r\n") in ("-- ", "--"):
                items.append(raw)
            else:
                joined = "".join(
                    p[:-1] if delsp and k < len(parts) - 1 else p for k, p in enumerate(parts)
                )
                if not joined.strip():
                    items.append(raw)
                else:
                    width = max(len(ln.rstrip("\r\n")) for ln in lines[i:j])
                    items.append(
                        {
                            "text": joined.strip(),
                            "raw": raw,
                            "lead": joined[: len(joined) - len(joined.lstrip())],
                            "trail": "",
                            "width": width,
                            "multi": True,
                            "eol": raw[len(raw.rstrip("\r\n")) :],
                        }
                    )
            i = j
        return items
    group: list[str] = []

    def flush():
        if not group:
            return
        bodies = [g.rstrip("\r\n") for g in group]
        raw = "".join(group)
        first = bodies[0]
        last = bodies[-1]
        items.append(
            {
                "text": " ".join(b.strip() for b in bodies),
                "raw": raw,
                "lead": first[: len(first) - len(first.lstrip())],
                "trail": last[len(last.rstrip()) :] if len(group) == 1 else "",
                "width": max(len(b.rstrip()) for b in bodies),
                "multi": len(group) > 1,
                "eol": raw[len(raw.rstrip("\r\n")) :],
            }
        )
        group.clear()

    for line in lines:
        body = line.rstrip("\r\n")
        if not body.strip() or body.lstrip().startswith(">") or body in ("-- ", "--"):
            flush()
            items.append(line)
            continue
        if group:
            prev = group[-1].rstrip("\r\n").rstrip()
            joins = len(prev) >= 60 and not body[:1].isspace() and not _LIST_MARK.match(body)
            if not joins:
                flush()
        group.append(line)
    flush()
    return items


def _render_plain(item: dict, new: str, flowed: bool, delsp: bool) -> str:
    eol = item["eol"] or ""
    sep = "\r\n" if "\r\n" in item["raw"] else "\n"
    if flowed:
        width = min(max(item["width"], 72), MAX_LINE - 2)
        chunks = textwrap.wrap(
            item["lead"] + new, width - 1, break_long_words=False, break_on_hyphens=False
        ) or [""]
        out = []
        for k, chunk in enumerate(chunks):
            if k < len(chunks) - 1:
                chunk = chunk + " " + (" " if delsp else "")
            else:
                chunk = chunk.rstrip(" ")
            if chunk.startswith((" ", ">", "From ")):
                chunk = " " + chunk  # space-stuffing (RFC 3676 section 4.4)
            out.append(chunk)
        return sep.join(out) + eol
    if not item["multi"]:
        return item["lead"] + new + item["trail"] + eol
    chunks = textwrap.wrap(
        new,
        max(item["width"] - len(item["lead"]), 20),
        break_long_words=False,
        break_on_hyphens=False,
    ) or [""]
    return sep.join(item["lead"] + c for c in chunks) + eol


# ------------------------------------------------------------------------ the filter


def _walk(data: bytes) -> tuple[_Part, list[_Slot], str]:
    data = bytes(data)
    first = data[: data.find(b"\n") + 1 if b"\n" in data else len(data)]
    if not _FIELD_START.match(first) or b"\x00" in data[:4096]:
        raise EmailRefusedError(
            "This file is not an email message (.eml): it must start with header lines "
            "such as From: and Subject:."
        )
    top = _parse_part(data, 0, len(data), 0, [0])
    eol = "\r\n" if b"\r\n" in data[: top.body_start] else "\n"
    slots: list[_Slot] = []
    subj = _subject_slot(data, top)
    if subj is not None:
        slots.append(subj)
    for part in _leaves(top):
        if not _translatable(part):
            continue
        text, codec, declared = _decode_body(data, part)
        for marker in _PGP_INLINE:
            if marker in text:
                raise EmailRefusedError(_SIGNED_MSG.format(kind="signed or encrypted"))
        kind = "html" if part.ctype == "text/html" else "plain"
        slot = _Slot(kind, part, text=text, codec=codec, declared=declared)
        if kind == "plain":
            fmt = str(part.headers.get_param("format") or "").lower()
            slot.flowed = fmt == "flowed"
            slot.delsp = str(part.headers.get_param("delsp") or "").lower() == "yes"
        slots.append(slot)
    return top, slots, eol


def _slot_units(slot: _Slot, *, enforce_caps: bool = True) -> list[TextUnit]:
    if slot.kind == "subject":
        return [TextUnit(p_index=0, text=slot.text.strip())] if slot.text.strip() else []
    if slot.kind == "html":
        return _HTML.parse(
            slot.text.encode("utf-8", "surrogateescape"), enforce_caps=enforce_caps
        ).units
    return [
        TextUnit(p_index=k, text=it["text"])
        for k, it in enumerate(
            i for i in _plain_items(slot.text, slot.flowed, slot.delsp) if isinstance(i, dict)
        )
    ]


def _mime_charset(codec: str) -> str:
    return {"cp1252": "windows-1252", "ascii": "us-ascii"}.get(codec, codec.replace("_", "-"))


def _delimiters(top: _Part) -> tuple[bytes, ...]:
    """`--boundary` of every multipart in the message (RFC 2046 section 5.1.1)."""
    out = []
    for p in _leaves(top):
        if not p.children:
            continue
        try:
            bnd = p.headers.get_param("boundary")
        except Exception:  # unreadable parameter: _parse_part found no children then
            bnd = None
        if isinstance(bnd, tuple):
            bnd = bnd[2]
        if isinstance(bnd, str) and bnd.isascii():
            out.append(b"--" + bnd.encode("ascii"))
    return tuple(out)


def _hits_delimiter(body: bytes, delims: tuple[bytes, ...]) -> bool:
    """True when a line of an encoded body could be read as a MIME delimiter: a
    translation must never be able to end its part and start a new one."""
    if not delims:
        return False
    return any(ln.lstrip(b"\r").startswith(delims) for ln in body.split(b"\n"))


def _encode_part(
    slot: _Slot, new_text: str, data: bytes, eol: str, delims: tuple[bytes, ...] = ()
) -> tuple[bytes, dict]:
    """Encode a rebuilt body with its original charset and transfer encoding, falling
    back to UTF-8 and/or quoted-printable when the translation does not fit, and to
    base64 when a line of the text would read as a MIME boundary delimiter (gate)."""
    edits: dict = {}
    codec = slot.codec
    try:
        payload = new_text.encode(codec)
        if not slot.declared and not payload.isascii():
            edits["charset"] = _mime_charset(codec)
    except UnicodeEncodeError:
        codec = "utf-8"
        payload = new_text.encode("utf-8")
        edits["charset"] = "utf-8"
        if slot.kind == "html":  # keep a <meta charset> truthful
            new_text = _DECL_RE.sub(lambda m: m.group(1) + "utf-8", new_text)
            payload = new_text.encode("utf-8")
    cte = _cte(slot.part)
    raw_old = data[slot.part.body_start : slot.part.end]
    if cte in ("7bit", "8bit", "binary"):
        too_long = any(len(ln) > MAX_LINE for ln in payload.splitlines())
        if (cte == "7bit" and not payload.isascii()) or too_long:
            cte = "quoted-printable"
            edits["cte"] = cte
        elif not _hits_delimiter(payload, delims):
            return payload, edits
        else:
            cte = "quoted-printable"
    if cte == "quoted-printable":
        body = quopri.encodestring(payload.replace(b"\r\n", b"\n"))
        body = body.replace(b"\n", eol.encode())
        if not _hits_delimiter(body, delims):
            if cte != _cte(slot.part):
                edits["cte"] = cte
            return body, edits
        cte = edits["cte"] = "base64"  # base64 has no '-': it can never form a delimiter
    body = base64.encodebytes(payload).replace(b"\n", eol.encode())
    if not raw_old.endswith((b"\n",)):
        body = body.rstrip(b"\r\n")
    return body, edits


@register
class EmailFilter(Filter):
    # #703: HTML parts go through HtmlFilter, which escapes raw <, > and & on export (quality
    # entity gate). A literal reference in a plain-text part is rare enough to share the rule.
    escapes_raw_text = True
    fmt = "eml"
    exts = ()

    def parse(
        self, data: bytes, provider: str | None = None, *, enforce_caps: bool = True
    ) -> ParseResult:
        _top, slots, _eol = _walk(data)
        units: list[TextUnit] = []
        parts_meta = []
        for slot in slots:
            local = _slot_units(slot, enforce_caps=enforce_caps)
            if slot.kind != "subject":
                parts_meta.append(
                    {
                        "type": slot.part.ctype,
                        "charset": slot.codec,
                        "cte": _cte(slot.part),
                        "flowed": slot.flowed,
                        "units": len(local),
                    }
                )
            for u in local:
                units.append(
                    TextUnit(
                        p_index=len(units), text=u.text, style=u.style, inline_map=u.inline_map
                    )
                )
                if enforce_caps:
                    check_segment_cap(len(units))
        if enforce_caps:
            check_unit_texts(u.text for u in units)
        if not any(u.text.strip() for u in units):
            raise EmptyDocumentError(
                "This email has no Subject or text body to translate (attachments are "
                "never translated)."
            )
        skeleton = {
            "format": "eml",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
            "parts": parts_meta,
        }
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._build(original, paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        return self._build(original, paragraph_texts, a11y.target_tag(target_locale) or None)

    def _build(self, original: bytes, paragraph_texts: dict, lang: str | None) -> bytes:
        data = bytes(original)
        top, slots, eol = _walk(data)
        beol = eol.encode()
        header_edits: dict[int, dict] = {}  # part.start -> edits
        bodies: dict[int, bytes] = {}  # part.body_start -> new body bytes
        parts = {p.start: p for p in _leaves(top)}
        delims = _delimiters(top)
        if lang:
            for p in parts.values():
                if p.headers.get("content-language") is not None:
                    header_edits.setdefault(p.start, {})["lang"] = lang
        base = 0
        for slot in slots:
            local = _slot_units(slot, enforce_caps=False)
            mine = {
                str(k): paragraph_texts[str(base + k)]
                for k in range(len(local))
                if str(base + k) in paragraph_texts
            }
            base += len(local)
            if slot.kind == "subject":
                new = mine.get("0")
                if local and new is not None and new != local[0].text:
                    header_edits.setdefault(top.start, {})["subject"] = _encode_subject(
                        slot, new, eol
                    )
                continue
            if slot.kind == "html":
                src = slot.text.encode("utf-8", "surrogateescape")
                if lang:
                    out = _HTML.build_localized(src, mine, lang)
                else:
                    out = _HTML.build(src, mine) if mine else src
                new_text = out.decode("utf-8", "surrogateescape")
            else:
                pieces = []
                k = 0
                for it in _plain_items(slot.text, slot.flowed, slot.delsp):
                    if isinstance(it, str):
                        pieces.append(it)
                        continue
                    new = mine.get(str(k))
                    k += 1
                    if new is None or new == it["text"]:
                        pieces.append(it["raw"])
                    else:
                        pieces.append(_render_plain(it, new, slot.flowed, slot.delsp))
                new_text = "".join(pieces)
            if new_text == slot.text:
                continue
            body, edits = _encode_part(slot, new_text, data, eol, delims)
            bodies[slot.part.body_start] = body
            if edits:
                header_edits.setdefault(slot.part.start, {}).update(edits)
        # splice: header blocks and bodies are disjoint ranges of the original
        cuts = []
        for start, edits in header_edits.items():
            p = parts[start]
            cuts.append((p.start, p.hdr_end, _edit_block(data[p.start : p.hdr_end], beol, edits)))
        for p in parts.values():
            if p.body_start in bodies and not p.children:
                cuts.append((p.body_start, p.end, bodies[p.body_start]))
        out = []
        pos = 0
        for s, e, new in sorted(cuts):
            out.append(data[pos:s])
            out.append(new)
            pos = e
        out.append(data[pos:])
        return b"".join(out)
