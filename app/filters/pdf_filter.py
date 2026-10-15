"""PDF filter (pypdfium2 for parse, typst for build).

parse(): for each page, extract the embedded text layer via pypdfium2 (PDFium). If
a page has little or no text (a scanned/image page), render it to a 150-dpi PNG and
OCR it with a vision-capable engine (services/ocr.py) - cloud or local Qwen3-VL.
Either way we emit paragraph units.

  THREAD-SAFETY (HIGH-1, gate rounds 2-3): PDFium is single-threaded and pypdfium2
  documents that ALL of its calls must be serialized; two threads inside PDFium at
  once corrupt its global state PERMANENTLY for the worker process (every later parse
  then fails). parse() is reachable off the event loop (connector job workers ingest
  attachments), so every pypdfium2 call is held behind the module-level _PDFIUM_LOCK.
  The lock is taken PER PAGE and RELEASED around the OCR network call: each page is
  extracted (or rendered to PNG bytes) UNDER the lock, then ocr.ocr_image is called with
  the lock released. Only ONE page's PNG is alive at a time (O(1) memory, NOT O(pages)),
  and a slow OCR round-trip never blocks other threads' parses. build() needs NO lock
  (typst is thread-safe).

  Text-layer round-trip limits (shared with the previous PDF engine, not a
  regression; the RENDER is correct in all these cases): PDFium's text extraction of an
  EXPORTED PDF (a) reverses/segments Arabic/Hebrew runs into visual order, (b) may
  duplicate a stacked combining-mark cluster in the ToUnicode stream (round tripping an
  exported PDF back through parse could triple such a cluster), and (c) drops text from
  the ToUnicode layer for a single UNBREAKABLE token tens of thousands of characters
  long (a pathological input; realistic long tokens — hashes, base64 — round-trip
  exactly). None affect a normally-authored source document; see the round-3 gate record.

  NOTE (LOW-5, behaviour change vs the previous PDF engine): a non-PDF file given a
  .pdf name is now REJECTED at parse (PdfiumError -> the upload surfaces' clean
  "corrupt file" error); the old engine silently converted a PNG-renamed-to-.pdf into
  a page and OCR'd it. Truncated PDFs likewise now raise instead of yielding a silent
  0-unit success.

build(): reflow the translated text into a clean, paginated PDF with **typst**
(Apache-2.0). typst does real HarfBuzz text shaping, bidi, and system-font FALLBACK, so
every script the product ships (CJK, Arabic/Hebrew RTL, Devanagari/Bengali conjuncts,
Thai marks, Cyrillic/Greek) renders correctly with the host's fonts and no per-script
font juggling. Layout-perfect round-trip isn't attempted; the value is translating the
content (incl. scanned docs) and getting a readable translated PDF back.

  Each unit is emitted as a typst STRING LITERAL inside `#par(text("..."))`, so the
  typst markup parser NEVER sees the translated text — no escaping of markup
  metacharacters, no line-start list/heading triggers, and (belt-and-braces) smartquotes
  are disabled. Only backslash, double-quote and control characters are escaped inside
  the literal, so the delivered PDF contains exactly the approved text: --flag, 10--20,
  ellipses, a leading "- "/"= ", #not-a-function, etc. all survive verbatim.

Fonts: typst uses the host's installed fonts plus its fallback chain. The container image
ships `fonts-noto-core` + `fonts-noto-cjk` (see Dockerfile) which cover every default
locale. Two optional env overrides:
  * LOC_TMS_PDF_FONT_DIR - a directory (or os.pathsep-separated list) of extra TTF/OTF
    files, passed to typst as font_paths;
  * LOC_TMS_PDF_FONT - a font family NAME to prefer (emitted as the first entry of
    `#set text(font: (...))` with fallback still on).
On a bare Linux host with neither the Noto packages nor an override, non-Latin scripts
may show missing glyphs; build() logs ONE warning naming the fix. See docs/embedded.md.

Both dependencies are permissively licensed (pypdfium2: Apache-2.0/BSD-3 with a bundled
PDFium; typst: Apache-2.0), so the default install carries no copyleft PDF engine.
See tests/verify_licenses.py.
"""

import io
import logging
import math
import os
import re
import sys
import threading

import typst

from app.filters.base import Filter, ParseResult, TextUnit, check_segment_cap, register
from app.services import ocr

log = logging.getLogger("loc_tms.pdf_filter")

# A page yielding fewer than this many characters of embedded text is treated as
# scanned and sent to OCR.
OCR_MIN_CHARS = 20

# Scanned pages are rendered at 150 dpi (PDF user space is 72 dpi) before OCR.
_OCR_RENDER_DPI = 150


def _ocr_render_scale(width_pt: float, height_pt: float) -> float | None:
    """The render scale for an OCR page, or None to skip the render (#272).

    The page size is chosen by the file (a 551-byte PDF can declare a 3000 x 3000 pt page,
    a 6250 x 6250 px bitmap at 150 dpi), so the scale is lowered until the longer edge
    fits config.PDF_OCR_MAX_PIXEL_DIM. A normal page (A4, Letter, A3) renders exactly as
    before. A page with no usable size is not rendered at all."""
    from app import config

    scale = _OCR_RENDER_DPI / 72
    try:
        longest = max(float(width_pt), float(height_pt))
    except (TypeError, ValueError):
        return None
    if not (longest > 0) or longest == float("inf"):
        return None
    cap = max(int(config.PDF_OCR_MAX_PIXEL_DIM), 1)
    if longest * scale > cap:
        scale = cap / longest
    return scale


# Serializes EVERY pypdfium2 (PDFium) call in this process — see the module docstring
# (HIGH-1). RLock so a future nested pypdfium2 helper on the same thread cannot
# self-deadlock. The ONLY pypdfium2 user in the app is PdfFilter.parse; the OCR network
# call is made OUTSIDE this lock.
_PDFIUM_LOCK = threading.RLock()

# One-time boot warning latch (MED-5): warn at most once per process when the host has no
# non-Latin font coverage configured.
_font_warned = False


def _paragraphs(text: str) -> list[str]:
    """Split a page's text into paragraph-ish blocks on blank lines."""
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    out = []
    for b in blocks:
        b = " ".join(line.strip() for line in b.splitlines() if line.strip())
        if b:
            out.append(b)
    return out


def _typst_literal(text: str) -> str:
    """One unit's text as the CONTENTS of a typst string literal. The only characters that
    must be escaped to stay inside the literal are backslash (-> ``\\\\``) and double quote
    (-> ``\\"``). Whitespace/control handling (MED-1, gate round 3): a real newline is kept
    (typst renders it as a line break that reads as a word separator); a TAB and every
    other C0 control character (U+0000-U+001F except newline) plus DEL is mapped to a
    SINGLE SPACE, because typst renders a tab/other control INSIDE a string literal as
    ZERO WIDTH — passing them through would silently FUSE adjacent words. Because the text
    lives inside a string passed to `text(...)`, the typst markup parser never interprets
    it, so nothing else needs escaping."""
    out = []
    for ch in text or "":
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append(ch)  # real newline -> line break -> reads as a word separator
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(" ")  # tab / other C0 / DEL render zero-width -> use a real space
        else:
            out.append(ch)
    return "".join(out)


def _font_family() -> str:
    """A font family NAME to prefer (LOC_TMS_PDF_FONT), or ''."""
    return (os.environ.get("LOC_TMS_PDF_FONT") or "").strip()


def _font_paths() -> list[str]:
    """Extra font directories from LOC_TMS_PDF_FONT_DIR (os.pathsep-separated), existing
    dirs only. Empty when unset."""
    raw = (os.environ.get("LOC_TMS_PDF_FONT_DIR") or "").strip()
    if not raw:
        return []
    return [p for p in raw.split(os.pathsep) if p and os.path.isdir(p)]


def _maybe_warn_fonts() -> None:
    """MED-5: on a Linux host with no configured non-Latin font coverage, warn ONCE per
    process that translated PDFs may show missing glyphs, naming the fix. Non-Linux hosts
    (dev machines) and any host with LOC_TMS_PDF_FONT_DIR / the Noto fonts present are
    silent."""
    global _font_warned
    if _font_warned:
        return
    _font_warned = True
    if os.name != "posix" or sys.platform == "darwin":
        return
    if _font_paths() or _font_family():
        return
    if os.path.isdir("/usr/share/fonts/truetype/noto") or os.path.isdir(
        "/usr/share/fonts/opentype/noto"
    ):
        return
    log.warning(
        "PDF export: no non-Latin fonts detected on this host; translated PDFs may show "
        "missing glyphs for non-Latin scripts. Install `fonts-noto-core fonts-noto-cjk` "
        "or set LOC_TMS_PDF_FONT_DIR to a directory of fonts."
    )


def _text_set(family: str, fallback: bool = True) -> str:
    """The `#set text(...)` line. Split out so the fallback-less counterfactual can reuse
    the REAL body/emit/compile path with only the font config swapped (MED-2)."""
    fb = "true" if fallback else "false"
    if family:
        fam_lit = _typst_literal(family)
        return f'#set text(size: 11pt, font: ("{fam_lit}",), fallback: {fb})\n'
    return f"#set text(size: 11pt, fallback: {fb})\n"


def _emit_units(ordered: list) -> str:
    """One typst paragraph per unit, each as a string literal inside `text(...)` so the
    markup parser never touches the translation."""
    return "\n".join(
        f'#par(text("{_typst_literal("" if t is None else str(t))}"))' for t in ordered
    )


def _lang_set(tag: str) -> str:
    """`#set text(lang: .., region: ..)` for a BCP 47 target tag (VPAT 504.2.2), or ""
    when the tag has no language typst accepts. typst takes an ISO 639 language and an
    optional ISO 3166-1 alpha-2 region; it writes them to the PDF catalog /Lang and the
    XMP dc:language, and derives the text direction from the language (rtl for ar/he/
    fa/ur/...). A numeric region (es-419) or a script subtag (zh-Hant) cannot be
    expressed, so only the language part is declared for those."""
    parts = (tag or "").split("-")
    lang = parts[0].lower()
    if not (2 <= len(lang) <= 3 and lang.isascii() and lang.isalpha()):
        return ""
    region = next((p for p in parts[1:] if len(p) == 2 and p.isascii() and p.isalpha()), "")
    if region:
        return f'#set text(lang: "{lang}", region: "{region.upper()}")\n'
    return f'#set text(lang: "{lang}")\n'


def _build_source(ordered: list, lang_tag: str = "") -> str:
    """The typst document: A4, 40pt margins, 11pt text with fallback on, 1.5x leading,
    smartquotes OFF, one string-literal paragraph per unit. `lang_tag` (the TARGET
    locale, BCP 47) adds the language declaration; empty keeps the historical source
    byte-for-byte (typst then defaults to "en")."""
    return (
        '#set page(paper: "a4", margin: 40pt)\n'
        + _text_set(_font_family(), fallback=True)
        + _lang_set(lang_tag)
        # leading 0.65em ~ 1.5x line spacing for 11pt text.
        + "#set par(leading: 0.65em)\n"
        # String literals already bypass typst's `--`/`...` shorthands; disable smartquotes
        # too so a straight quote is never curled (belt-and-braces).
        + "#set smartquote(enabled: false)\n"
        + _emit_units(ordered)
        + "\n"
    )


@register
class PdfFilter(Filter):
    fmt = "pdf"
    passthrough_untouched = False  # B-bytes: the deliverable is a different file

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        # pypdfium2 is imported on first use (test speed, 2026-10-08): it loads the PDFium
        # library, and every app and CLI process imports this filter through the registry.
        import pypdfium2 as pdfium

        # PAGE-AT-A-TIME (HIGH-1, gate round 3): the PDFium lock is taken PER PAGE, and the
        # OCR call happens OUTSIDE it, so at most ONE page's PNG is alive at any moment.
        # (An earlier fix buffered every scanned page's PNG before the first OCR call — a
        # 100-page scan held ~228 MiB regardless of the upload size; O(pages), not O(1).)
        # The PdfDocument handle stays open across the per-page lock releases: the RLock
        # guards PDFium *calls*, not handle lifetime, and the whole document is never
        # touched by two threads at once.
        units: list[TextUnit] = []
        pages_meta = []
        idx = 0
        with _PDFIUM_LOCK:
            # PdfDocument raises pypdfium2.PdfiumError on a corrupt / non-PDF / empty
            # stream; the upload path (app/web/documents.py) maps any parse exception to
            # a clean "?err=parse" user-facing error, so we let it propagate.
            pdf = pdfium.PdfDocument(data)
            npages = len(pdf)
        # #272: every page with no text layer is one bitmap render and one OCR call, so the
        # page count is capped. Refused before any page is rendered.
        from app import config
        from app.filters.base import InputLimitError

        if npages > config.PDF_MAX_PAGES:
            with _PDFIUM_LOCK:
                pdf.close()
            raise InputLimitError(
                f"This PDF has {npages} pages; the limit is {config.PDF_MAX_PAGES}. "
                "Split it into smaller files and upload those."
            )
        try:
            # Preflight only pages that need OCR, using the same size/scale and ceil
            # rounding as PDFium. Refuse the whole document before the first bitmap or
            # OCR call. Keep only scales here, not every page's text or PNG in memory.
            scales = []
            total_pixels = 0
            # #571 gate: text extraction costs time per page OBJECT, and pages may share
            # one compressed content stream, so a 0.5 MB PDF of 20 pages x 200000 text
            # objects took 48 s (1000 such pages: most of an hour). Counting a loaded
            # page's objects is free; eighty per allowed segment (4000000 at the
            # default, about twice a 1000-page book set glyph by glyph) bounds the work
            # before any text page is built.
            objects_left = 80 * config.MAX_DOCUMENT_SEGMENTS
            for pno in range(npages):
                with _PDFIUM_LOCK:
                    page = pdf[pno]
                    try:
                        objects_left -= pdfium.raw.FPDFPage_CountObjects(page.raw)
                        if objects_left < 0:
                            raise InputLimitError(
                                "This PDF has far more text and drawing objects than "
                                "MAX_DOCUMENT_SEGMENTS allows "
                                f"({config.MAX_DOCUMENT_SEGMENTS} segments). "
                                "Split it into smaller files."
                            )
                        textpage = page.get_textpage()
                        try:
                            needs_ocr = (
                                len((textpage.get_text_range() or "").strip()) < OCR_MIN_CHARS
                            )
                        finally:
                            textpage.close()
                        width, height = page.get_size()
                        scale = _ocr_render_scale(width, height) if needs_ocr else None
                        scales.append(scale)
                        if scale is not None:
                            total_pixels += math.ceil(width * scale) * math.ceil(height * scale)
                            if total_pixels > config.PDF_OCR_MAX_TOTAL_PIXELS:
                                raise InputLimitError(
                                    "This PDF exceeds the total OCR render pixel limit "
                                    f"({config.PDF_OCR_MAX_TOTAL_PIXELS} pixels). "
                                    "Split it into smaller files."
                                )
                    finally:
                        page.close()
            for pno in range(npages):
                png = None
                # PDFium work for THIS page ONLY, under the lock.
                with _PDFIUM_LOCK:
                    page = pdf[pno]
                    try:
                        textpage = page.get_textpage()
                        try:
                            text = (textpage.get_text_range() or "").strip()
                        finally:
                            textpage.close()
                        # #272: the render scale is bounded by the page's own size.
                        scale = scales[pno]
                        if scale is not None:
                            bitmap = page.render(scale=scale)
                            try:
                                png_buf = io.BytesIO()
                                bitmap.to_pil().save(png_buf, format="PNG")
                                png = png_buf.getvalue()
                            finally:
                                bitmap.close()
                    finally:
                        page.close()  # release the native page handle
                # OCR OUTSIDE the lock (network call); only this one page's PNG is alive,
                # and it is dropped before the next page renders.
                ocr_used = False
                if png is not None:
                    text = ocr.ocr_image(png, provider).strip()
                    ocr_used = True
                    png = None
                pages_meta.append({"page": pno, "ocr": ocr_used})
                for para in _paragraphs(text or ""):
                    units.append(TextUnit(p_index=idx, text=para))
                    idx += 1
                    check_segment_cap(idx)  # #571 gate: stop before the remaining pages
        finally:
            with _PDFIUM_LOCK:
                pdf.close()  # release the native document handle
        skeleton = {
            "format": "pdf",
            "pages": pages_meta,
            "ocr_pages": sum(1 for m in pages_meta if m["ocr"]),
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._compile(paragraph_texts, "")

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        from app.filters import a11y

        return self._compile(paragraph_texts, a11y.target_tag(target_locale))

    def _compile(self, paragraph_texts: dict, lang_tag: str) -> bytes:
        _maybe_warn_fonts()
        ordered = [paragraph_texts[k] for k in sorted(paragraph_texts, key=lambda x: int(x))]
        # The one-argument call is kept for the untagged path (a test swaps
        # _build_source for a one-argument counterfactual writer).
        text = _build_source(ordered, lang_tag) if lang_tag else _build_source(ordered)
        source = text.encode("utf-8")
        # typst-py compiles an in-memory source (bytes) with no filesystem access, so no
        # tempdir/root is needed; the source is fully generated + string-escaped here,
        # never a path or user-controlled function call. font_paths augments system fonts.
        # typst is thread-safe, so build() takes NO PDFium lock.
        paths = _font_paths()
        if paths:
            return typst.compile(source, font_paths=paths)
        return typst.compile(source)
