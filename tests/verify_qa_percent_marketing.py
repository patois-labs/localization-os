"""A percent sign glued to letters after a number is marketing copy, not a printf placeholder.

Real workflow run (demo-reel/locworld FALL-CAMPAIGN-VIDEO.md): "20%OFF" / "50%Rabatt" read as
`%O` / `%R` on the editor's live "tags" badge and the segment was held as a structural error.
Real placeholders (%s %d %1$s %(name)s %@ %.2f) stay detected.

Hermetic. Final line "QA PERCENT MARKETING OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_pctmkt_")

from app.services import qa
from app.services.segment import extract_inline_tags


def _fail(msg):
    print("FAIL:", str(msg).encode("ascii", "backslashreplace").decode("ascii"))
    sys.exit(1)


def _tag_flags(src, tgt):
    return [f for f in qa.check_segment(src, tgt, "fr-FR") if f["type"] == "tags"]


def main():
    for src, tgt in [
        ("Get 20%OFF today", "Profitez de 20% de remise"),
        ("Get 20%off today", "Profitez de 20% de remise"),
        ("Save 50%Rabatt now", "Save 50% now"),
        ("Jetzt 50%Rabatt", "Now 50 percent off"),
    ]:
        if extract_inline_tags(src):
            _fail(f"marketing copy read as a placeholder: {src!r} -> {extract_inline_tags(src)}")
        if _tag_flags(src, tgt):
            _fail(f"marketing copy held as a tag error: {src!r} -> {_tag_flags(src, tgt)}")
    for ph in ["%s", "%d", "%1$s", "%2$d", "%(name)s", "%@", "%.2f", "%05d", "%ld"]:
        src = f"Hello {ph} there"
        if extract_inline_tags(src) != [ph]:
            _fail(f"real placeholder lost: {ph!r} -> {extract_inline_tags(src)}")
        if not _tag_flags(src, "Bonjour la"):
            _fail(f"dropped placeholder not flagged: {ph!r}")
        if _tag_flags(src, f"Bonjour {ph} la"):
            _fail(f"kept placeholder flagged: {ph!r}")
    # gate: a dropped or added DUPLICATE placeholder is still a tags flag, with or without a
    # target locale (the link-locale pairing must not swallow a count-only divergence)
    for loc in ("fr-FR", None):
        flags = [
            f for f in qa.check_segment("Got %s and %s", "Recu %s", loc) if f["type"] == "tags"
        ]
        if not flags or "%s" not in flags[0]["message"]:
            _fail(f"dropped duplicate %s not flagged (locale {loc!r}): {flags}")
        if not [f for f in qa.check_segment("Got %s", "Recu %s %s", loc) if f["type"] == "tags"]:
            _fail(f"added duplicate %s not flagged (locale {loc!r})")
    if [f for f in qa.check_segment("%1$s of %2$d", "%2$d de %1$s", "fr") if f["type"] == "tags"]:
        _fail("a reordered placeholder pair was flagged")
    # a real placeholder next to a percent-number still counts
    if extract_inline_tags("20%OFF for %s") != ["%s"]:
        _fail("placeholder beside marketing percent")
    print("QA PERCENT MARKETING OK")


if __name__ == "__main__":
    main()
