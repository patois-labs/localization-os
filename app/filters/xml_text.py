"""Text normalization at XML format boundaries; stored translations remain intact.

Documented in: docs/architecture/translation-pipeline.md
"""

import re

_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def xml_target_text(text: str) -> str:
    """Keep XML 1.0 characters, interpreting pasted Word breaks as newlines."""
    return _ILLEGAL_XML.sub("", text.replace("\x0b", "\n").replace("\x0c", "\n"))


class XmlTargetError(ValueError):
    """A target still cannot be written, with its reconstruction unit identified."""
