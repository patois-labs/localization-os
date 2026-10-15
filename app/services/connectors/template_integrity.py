"""Delivery-time template fidelity, including entity-encoded delimiters.

Documented in: docs/architecture/connectors.md
"""

import re
from collections import Counter
from html import unescape


class DelimitedSpans:
    """Regex-compatible forward scanner with at most one failed search per closer.

    finditer/sub/search/findall are the operations used by connector HTML filters.
    Failed unclosed openers never restart a scan of the same suffix.
    """

    def __init__(self, openers, closers, *, escaped=False):
        self.openers = re.compile(openers)
        self.closers = closers
        self.escaped = escaped

    def finditer(self, text):
        pos = 0
        exhausted = set()
        while match := self.openers.search(text, pos):
            closer = self.closers.get(match.group(0), self.closers.get(None))
            end = -1 if closer in exhausted else text.find(closer, match.end())
            if end < 0:
                exhausted.add(closer)
                # A different family can overlap this unclosed opener: {{# ... #}.
                pos = match.start() + 1
                continue
            end += len(closer)
            if self.escaped and text[end : end + 1] == "]":
                end += 1
            yield re.compile(r"[\s\S]*").match(text, match.start(), end)
            pos = end

    def findall(self, text):
        return [m.group(0) for m in self.finditer(text)]

    def search(self, text):
        return next(self.finditer(text), None)

    def sub(self, replacement, text):
        pieces = []
        pos = 0
        for match in self.finditer(text):
            pieces.extend(
                (
                    text[pos : match.start()],
                    replacement(match) if callable(replacement) else replacement,
                )
            )
            pos = match.end()
        pieces.append(text[pos:])
        return "".join(pieces)


HUBL_SPANS = DelimitedSpans(r"\{\{|\{%|\{#", {"{{": "}}", "{%": "%}", "{#": "#}"})
MERGE_SPANS = DelimitedSpans(r"\{\{", {"{{": "}}"})
# WordPress forbids these characters in registered names. Attributes may contain
# anything up to the closing bracket, including positional arguments and newlines.
_WP_NAME = r"\[\[?/?([^\s\[\]<>&/=\x00-\x20]+)"
SHORTCODE_SPANS = DelimitedSpans(_WP_NAME, {None: "]"}, escaped=True)
_WP_TAGS = re.compile(_WP_NAME)
_INERT = re.compile(r"\{%-?\s*(?:raw\b|if\s+false\b)", re.I)


def template_spans_match(source: str, target: str, pattern) -> bool:
    """Require fidelity at raw and decoded layers; refuse excessive encoding depth.

    Raw/false wrappers also require span order, so inert spans cannot become live.
    Ordinary independent spans may still reorder. Payload bytes are never changed.
    """
    source, target = source or "", target or ""
    for _ in range(8):
        left, right = pattern.findall(source), pattern.findall(target)
        if Counter(left) != Counter(right):
            return False
        if (_INERT.search(source) or _INERT.search(target)) and left != right:
            return False
        if pattern is SHORTCODE_SPANS and (
            Counter(_WP_TAGS.findall(source)) != Counter(_WP_TAGS.findall(target))
        ):
            return False
        decoded_source, decoded_target = unescape(source), unescape(target)
        if (decoded_source, decoded_target) == (source, target):
            return True
        source, target = decoded_source, decoded_target
    return False
