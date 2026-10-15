"""Plain-text filter.

parse(): one TextUnit per non-empty line (leading indentation is remembered, not
translated). Blank lines and indentation are part of the skeleton.

build(): re-walk the original line-by-line and swap in the translated lines,
re-attaching each line's original leading whitespace; blank lines are preserved.
"""

from app import config
from app.filters.base import (
    Filter,
    ParseResult,
    TextUnit,
    check_segment_cap,
    decode_text_lossless,
    encode_like,
    note_source_encoding,
    read_text_upload,
    register,
    text_envelope,
)


def _lead(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


@register
class TextFilter(Filter):
    fmt = "txt"
    exts = ("text",)

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        # utf-8-sig (matching json/po/srt/glossary filters): strip a leading UTF-8 BOM
        # so a BOM-prefixed .txt does not leave a stray U+FEFF on the first source segment.
        # #509: a Windows-1252 file is converted, not refused (decode_text_lossless at
        # build reads it the same way); an unreadable one is refused with a clear message.
        s, enc = read_text_upload(data, sig=True)
        units: list[TextUnit] = []
        idx = 0
        for line in s.splitlines():
            if line.strip():
                units.append(TextUnit(p_index=idx, text=line.strip()))
                idx += 1
                if idx > config.MAX_DOCUMENT_SEGMENTS:
                    check_segment_cap(idx)  # #571: stop before building the rest
        skeleton = {
            "format": "txt",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        # keepends: every line keeps its own terminator (CRLF stays CRLF, the final
        # newline stays) and undecodable bytes survive untouched (#509).
        out: list[str] = []
        k = 0
        for raw in decode_text_lossless(original, sig=True).splitlines(keepends=True):
            parts = raw.splitlines()
            body = parts[0] if parts else ""
            ending = raw[len(body) :]
            if body.strip():
                new = paragraph_texts.get(str(k), body.strip())
                out.append(_lead(body) + new + ending)
                k += 1
            else:
                out.append(raw)
        # B-bytes: the original's BOM and encoding come back (Windows-1252 when it fits).
        return encode_like("".join(out), text_envelope(original))
