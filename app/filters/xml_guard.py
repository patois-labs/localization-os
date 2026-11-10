"""Neutral home for the hardened lxml parser used on untrusted XML uploads.

Lives in app/filters (always-on core) so a core file filter (TTML, ...) can share the SAME
parser posture as the feature-gated vendor XLIFF path without importing a `vendors` service
module (verify_features check 11, cross-feature reach). services/vendor_xliff delegates to
this factory, so the two parse paths cannot drift.
"""

from __future__ import annotations

import re

from lxml import etree


def make_hardened_parser() -> etree.XMLParser:
    """The hardened lxml parser for untrusted XML: NO external entity resolution, NO
    network, NO DTD load, NO huge-tree. Combined with a DOCTYPE prescan + post-parse
    docinfo assert, an XXE / billion-laughs payload cannot fetch a file, expand an entity,
    or exhaust memory."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )


# #792: libxml2 takes quadratic time in the number of attributes (and namespace
# declarations) of ONE start tag: 40,000 `xmlns:pN` + `pN:a` pairs held a worker for 27 s
# in a 1.4 MB file, inside etree.fromstring, which no tree cap can interrupt. The budget is
# checked on the raw bytes before the parse (plain attributes are quadratic too: 40,000 in
# 0.4 MB took 9 s). Real tags carry a few dozen attributes (the widest in 5,000 real XML
# documents: 285). Only a run of 5 x MAX_TAG_ATTRIBUTES characters
# without a '<' can hold such a tag (an attribute is at least ` a=""`, and '<' cannot occur
# in an attribute value); in each, the attributes of the start tag itself are counted, so
# text that merely looks like attributes (escaped HTML in a value) is never counted.
MAX_TAG_ATTRIBUTES = 2048
_RUN = 5 * MAX_TAG_ATTRIBUTES
_ATTRS = r"""<[^\s/>!?<]+((?:\s+[^\s=/>]+\s*=\s*(?:"[^"]*"|'[^']*'))*)"""
_ONE = r"""\s+[^\s=/>]+\s*=\s*(?:"[^"]*"|'[^']*')"""
_LONG = "<[^<]{" + str(_RUN) + ",}"
_LONG_STR, _LONG_BYTES = re.compile(_LONG), re.compile(_LONG.encode())
_ATTRS_STR, _ATTRS_BYTES = re.compile(_ATTRS), re.compile(_ATTRS.encode())
_ONE_STR, _ONE_BYTES = re.compile(_ONE), re.compile(_ONE.encode())


def _wide_text(raw: bytes) -> str | None:
    """A UTF-16 document as text (its ASCII markup is invisible to a byte scan)."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    if raw[:4] == b"<\x00?\x00" or raw[:2] == b"<\x00":
        return raw.decode("utf-16-le", "replace")
    if raw[:4] == b"\x00<\x00?" or raw[:2] == b"\x00<":
        return raw.decode("utf-16-be", "replace")
    return None


def tag_budget_exceeded(raw: bytes | str) -> bool:
    """True when one start tag has more than MAX_TAG_ATTRIBUTES attributes (namespace
    declarations included)."""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = bytes(raw)
        wide = _wide_text(raw)
        if wide is not None:
            raw = wide
    if isinstance(raw, str):
        long_re, attrs_re, one_re = _LONG_STR, _ATTRS_STR, _ONE_STR
    else:
        long_re, attrs_re, one_re = _LONG_BYTES, _ATTRS_BYTES, _ONE_BYTES
    if len(raw) < _RUN:
        return False
    for m in long_re.finditer(raw):
        tag = attrs_re.match(raw, m.start())
        if tag is None:
            continue
        for count, _a in enumerate(one_re.finditer(raw, tag.start(1), tag.end(1)), 1):
            if count > MAX_TAG_ATTRIBUTES:
                return True
    return False


def check_tag_budget(raw: bytes | str, *, what: str = "This XML file") -> None:
    """Refuse a file with more than MAX_TAG_ATTRIBUTES attributes (namespace declarations
    included) in one start tag, before any parse. Raises InputLimitError (a ValueError)."""
    from app.filters.base import InputLimitError

    if tag_budget_exceeded(raw):
        raise InputLimitError(
            f"{what} has a start tag with more than {MAX_TAG_ATTRIBUTES} attributes or "
            "namespace declarations; refused (it would take far too long to read)."
        )
