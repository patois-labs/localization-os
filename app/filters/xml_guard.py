"""Neutral home for the hardened lxml parser used on untrusted XML uploads.

Lives in app/filters (always-on core) so a core file filter (TTML, ...) can share the SAME
parser posture as the feature-gated vendor XLIFF path without importing a `vendors` service
module (verify_features check 11, cross-feature reach). services/vendor_xliff delegates to
this factory, so the two parse paths cannot drift.
"""

from __future__ import annotations

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
