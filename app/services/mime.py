"""Per-format download MIME types -- a pure ``Document.fmt`` -> Content-Type lookup.

This is DOMAIN DATA (a format->media-type map), not a web concern, so it lives in the
kernel. Relocated here from ``app/web/deps.py`` in composable-core phase 4 (K1) to cut the
one reverse import edge ``app/services/delivery.py`` -> ``app.web.deps`` (a core module may
not import a shell). ``app/web/deps.py`` re-exports ``mime_for_fmt`` (wrap-don't-move) so its
own web/api/mcp callers stay byte-identical; ``delivery.py`` now imports it from here.
"""

# Keyed by Document.fmt; unknown formats fall back to a generic binary stream. Shared by the
# UI export route (app/web/documents.py), the API download route (app/web/api.py), the image
# routes (app/web/images.py) and the export bundler (app/services/delivery.py) so they never
# drift (BUG-2).
MIME_BY_FMT = {
    "catkeys": "text/plain; charset=utf-8",
    "desktop": "application/x-desktop",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "eml": "message/rfc822",
    "epub": "application/epub+zip",
    "dxf": "image/vnd.dxf",  # IANA; ASCII DXF keeps its own encoding
    "pdf": "application/pdf",
    "html": "text/html; charset=utf-8",
    "htm": "text/html; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "json5": "application/json5; charset=utf-8",  # not IANA-registered; de facto
    "jsonc": "application/jsonc; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "mif": "application/vnd.mif",
    "adoc": "text/asciidoc; charset=utf-8",
    "mdx": "text/markdown; charset=utf-8",  # MDX has no registered media type
    "wiki": "text/x-wiki; charset=utf-8",
    "dokuwiki": "text/plain; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "po": "text/x-gettext-translation; charset=utf-8",
    "srt": "application/x-subrip",
    "sbv": "text/plain; charset=utf-8",
    "sub": "text/plain; charset=utf-8",
    "toml": "application/toml; charset=utf-8",
    "stf": "text/plain; charset=utf-8",
    "vtt": "text/vtt; charset=utf-8",
    "ttml": "application/ttml+xml",
    "dfxp": "application/ttml+xml",
    "scc": "text/plain",  # Scenarist SCC (ASCII hex)
    "stl": "application/octet-stream",  # EBU STL (binary)
    "xaml": "application/xaml+xml",
    "xmb": "application/xml",
    "xtb": "application/xml",
    "xliff": "application/xliff+xml",
    "xlf": "application/xliff+xml",
    "sdlrpx": "application/zip",
    "sdlppx": "application/zip",
    "mqxlz": "application/zip",
    "xlz": "application/zip",
    "wsxz": "application/zip",
    "xml": "application/xml",
    "dita": "application/xml",
    "docbook": "application/xml",
    "ditamap": "application/xml",
    "bookmap": "application/xml",
    "dbk": "application/xml",
    "sdlxliff": "application/xliff+xml",
    "mqxliff": "application/xliff+xml",
    "mxliff": "application/xliff+xml",
    # XML software resources (Batch 2): all served as XML text.
    "resx": "application/xml; charset=utf-8",
    "resw": "application/xml; charset=utf-8",
    "wxl": "application/xml; charset=utf-8",
    "ts": "application/xml; charset=utf-8",
    "ttx": "application/xml",
    "txlf": "application/xliff+xml",
    "txml": "application/xml",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    # OpenDocument (format Batch 12); the download keeps the uploaded extension.
    "odt": "application/vnd.oasis.opendocument.text",
    "ott": "application/vnd.oasis.opendocument.text-template",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "ots": "application/vnd.oasis.opendocument.spreadsheet-template",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "otp": "application/vnd.oasis.opendocument.presentation-template",
    "odg": "application/vnd.oasis.opendocument.graphics",
    "otg": "application/vnd.oasis.opendocument.graphics-template",
    # Server-page templates (format Batch 16): a template is source code, never a page
    # to render, so it downloads as plain text.
    "php": "text/plain; charset=utf-8",
    "phtml": "text/plain; charset=utf-8",
    "jsp": "text/plain; charset=utf-8",
    "jspf": "text/plain; charset=utf-8",
    "aspx": "text/plain; charset=utf-8",
    "ascx": "text/plain; charset=utf-8",
    "erb": "text/plain; charset=utf-8",
    "shtml": "text/plain; charset=utf-8",
    "shtm": "text/plain; charset=utf-8",
    "stm": "text/plain; charset=utf-8",
    # MadCap Flare project zip (app/filters/flare_filter.py): the export is the same zip.
    "flprjzip": "application/zip",
    # Office macro / template / PowerPoint siblings (format Batch 4). The DOWNLOAD keeps
    # the uploaded extension, so a macro file is never served as its macro-free sibling.
    "xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
    "xltm": "application/vnd.ms-excel.template.macroEnabled.12",
    "docm": "application/vnd.ms-word.document.macroEnabled.12",
    "dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "dotm": "application/vnd.ms-word.template.macroEnabled.12",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
    "potx": "application/vnd.openxmlformats-officedocument.presentationml.template",
    "potm": "application/vnd.ms-powerpoint.template.macroEnabled.12",
    "ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
    "ppsm": "application/vnd.ms-powerpoint.slideshow.macroEnabled.12",
    "dtd": "application/xml-dtd; charset=utf-8",
    "ftl": "text/plain; charset=utf-8",
    # Key-value software resources (file-format Batch 1). No charset: .properties may be
    # ISO-8859-1, .strings and .ini may be UTF-16; the file keeps its source encoding.
    "properties": "text/x-java-properties",
    "strings": "text/plain",
    "ini": "text/plain",
    "resjson": "application/json; charset=utf-8",
    "xsl": "application/xslt+xml",  # RFC 3023 / XSLT 2.0 appendix
    "xslt": "application/xslt+xml",
    "yaml": "application/yaml; charset=utf-8",  # RFC 9512
    "yml": "application/yaml; charset=utf-8",
    # Windows resource script: no charset, the file keeps UTF-16, UTF-8 or its ANSI code page.
    "rc": "text/plain",
    # Adobe InDesign Markup Language package (PRONOM fmt/1184).
    "idml": "application/vnd.adobe.indesign-idml-package",
    "vsdx": "application/vnd.ms-visio.drawing.main+xml",
    "vsdm": "application/vnd.ms-visio.drawing.macroEnabled.main+xml",
    # Apple resources (file-format Batch 8): plists may be XML or binary.
    "plist": "application/x-plist",
    "stringsdict": "application/x-plist",
    "xcstrings": "application/json; charset=utf-8",
    # JS/TS locale modules: served as text, never as an executable script type.
    "js": "text/plain; charset=utf-8",
    "mjs": "text/plain; charset=utf-8",
    "cjs": "text/plain; charset=utf-8",
    "mts": "text/plain; charset=utf-8",
    "cts": "text/plain; charset=utf-8",
    # Adobe InCopy / WoodWing article: one XML file (no registered vendor type).
    "icml": "application/xml",
    # QuarkXPress Tags: no charset, the file keeps the encoding its <e> tag names.
    "xtg": "text/plain",
    "tag": "text/plain",
    "rst": "text/x-rst; charset=utf-8",  # reStructuredText (docutils)
    "tex": "application/x-tex; charset=utf-8",
    # Image localization (Beta) -- SPEC-image-localization 3.3.
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "bmp": "image/bmp",
    "gif": "image/gif",
    "webp": "image/webp",
    # Apple Xcode localization catalog, uploaded zipped (file-format Batch 16).
    "xcloc": "application/zip",
}
DEFAULT_MIME = "application/octet-stream"


def mime_for_fmt(fmt: str) -> str:
    """Content-Type for a Document.fmt, defaulting to a generic binary stream."""
    return MIME_BY_FMT.get((fmt or "").lower(), DEFAULT_MIME)


# A source format whose translated deliverable is a DIFFERENT file type: an Angular XMB
# source exports an XTB translation bundle (angular.dev/guide/i18n/translation-files).
EXPORT_EXT_BY_FMT = {"xmb": "xtb"}


def export_ext_for(fmt: str) -> str:
    """The file extension of a translated export for a Document.fmt (usually the same)."""
    return EXPORT_EXT_BY_FMT.get((fmt or "").lower(), fmt)
