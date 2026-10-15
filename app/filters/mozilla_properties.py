"""Mozilla .properties dialect (detection rule for the shared `.properties` filter).

Mozilla (Firefox, Thunderbird, SeaMonkey) ships .properties string bundles that use the
java.util.Properties grammar (`key = value`, `#`/`!` comments, backslash continuation,
`\\n` `\\t` `\\uXXXX` escapes), but the file is UTF-8, never ISO-8859-1:
  https://firefox-source-docs.mozilla.org/l10n/fluent/review.html (legacy formats) and
  https://developer.mozilla.org/en-US/docs/Mozilla/Localization/Localization_content_best_practices
  (UTF-8, LOCALIZATION NOTE comments, %S / %1$S placeholders, PluralForm `;` variants).
The runtime parser (nsPersistentProperties) reads UTF-8 and also accepts `\\uXXXX`.

There is ONE `.properties` handler (file-format Batch 1, `properties_filter.py`, fmt
"properties"); the document's stored fmt is its extension and is reused as the export
file extension, so a second fmt for the same extension cannot exist. Mozilla files are
therefore a DIALECT of that handler, chosen by content:

  A .properties file is Mozilla-dialect when it decodes as UTF-8, no value is a Java
  MessageFormat pattern (`{0}`, `{1,number}`), and it carries at least one Mozilla
  marker: the MPL 2.0 licence header, a `LOCALIZATION NOTE` comment, a Mozilla string
  placeholder `%S` / `%1$S`, or a PluralForm `#1` placeholder.

What the dialect changes (and nothing else): translated values are written as literal
UTF-8 even when the source file was all ASCII (Pontoon and compare-locales expect
UTF-8 text, not `\\uXXXX` runs), and MessageFormat apostrophe doubling never applies.
A file that is not valid UTF-8 is never Mozilla-dialect (it stays Java ISO-8859-1).

PluralForm values (`#1 file;#1 files`) stay ONE unit with their `;` separators: the
number of forms depends on the target locale's PluralForm rule, which a document
workflow cannot re-shape; the translator keeps one form per `;` slot.
"""

from __future__ import annotations

import re

_MF_ARG = re.compile(r"\{\s*\d+\s*[,}]")
_MARKERS = re.compile(
    r"Mozilla Public\s+License"
    r"|LOCALIZATION NOTE"
    r"|%(?:\d+\$)?S(?![A-Za-z])"
    r"|(?<![&\w])#[1-9](?!\d)"
)


def looks_mozilla(text: str) -> bool:
    """True when decoded UTF-8 .properties text is the Mozilla dialect (rule above)."""
    if _MF_ARG.search(text):
        return False
    return _MARKERS.search(text) is not None


def output_mode(text: str) -> str:
    """The Batch 1 PropertiesFilter output mode for a file that decoded as UTF-8:
    "utf-8" (write literal UTF-8) for a file with non-ASCII text or a Mozilla-dialect
    file, else "ascii" (the native2ascii convention: `\\uXXXX` for everything above
    U+007E)."""
    if any(ord(c) > 0x7F for c in text) or looks_mozilla(text):
        return "utf-8"
    return "ascii"
