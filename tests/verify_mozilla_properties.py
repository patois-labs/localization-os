"""Mozilla .properties dialect rule (file-format Batch 11): app/filters/mozilla_properties.py.

The dialect detection rule, and (once the Batch 1 `.properties` filter is on the
branch) that a Mozilla-dialect file's translations are written as literal UTF-8 while
a plain Java file keeps its native2ascii `\\uXXXX` output and MessageFormat rules.

Hermetic + offline. Final line: "MOZILLA PROPERTIES OK".
"""

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mozprops_")

from app.filters.mozilla_properties import looks_mozilla, output_mode

MOZ = (
    "# This Source Code Form is subject to the terms of the Mozilla Public\n"
    "# License, v. 2.0.\n"
    "\n"
    "# LOCALIZATION NOTE (downloadsTitleFiles): Semicolon-separated list of plural forms.\n"
    "# #1 number of files; #2 overall download percent\n"
    "downloadsTitleFiles=#1 file - Downloads;#1 files - Downloads\n"
    "fileExists=The file %S already exists.\n"
    "copyOf=%1$S of %2$S\n"
)
JAVA = "# Application messages\ngreeting=Hello {0}, it''s {1,number} o''clock\nfarewell = Goodbye\n"


def test_detection_rule():
    assert looks_mozilla(MOZ)
    assert not looks_mozilla(JAVA)
    # each marker on its own is enough
    assert looks_mozilla("a=b\n# LOCALIZATION NOTE (a): x\n")
    assert looks_mozilla("a=Open %S\n")
    assert looks_mozilla("a=Open %2$S now\n")
    assert looks_mozilla("a=#1 tab;#1 tabs\n")
    assert looks_mozilla("# Mozilla Public License\na=b\n")
    # Java-only shapes are never Mozilla, even with a Mozilla marker
    assert not looks_mozilla("a=Open %S {0}\n")
    assert not looks_mozilla("a=Open {1,number}\n# LOCALIZATION NOTE\n")
    # lower-case %s, &#1; and #10 are not markers
    assert not looks_mozilla("a=Open %s\nb=&#1;\nc=#10 items\n")
    print("  [mozprops] detection rule OK")


def test_output_mode():
    assert output_mode(MOZ) == "utf-8"
    assert output_mode(JAVA) == "ascii"
    assert output_mode("a=café\n") == "utf-8"
    print("  [mozprops] output mode (UTF-8 for Mozilla, ASCII native2ascii for Java) OK")


def test_with_batch1_filter():
    """Runs when the Batch 1 `.properties` filter is on the branch (after its merge)."""
    if importlib.util.find_spec("app.filters.properties_filter") is None:
        print("  [mozprops] Batch 1 .properties filter not on this branch: skipped")
        return
    from app.filters import get_filter

    f = get_filter("properties")
    units = f.parse(MOZ.encode()).units
    texts = [u.text for u in units]
    assert "The file %S already exists." in texts, texts
    k = texts.index("The file %S already exists.")
    out = f.build(MOZ.encode(), {str(k): "Die Datei %S existiert bereits (größer)."})
    assert "fileExists=Die Datei %S existiert bereits (größer).\n".encode() in out, out
    assert b"\\u00" not in out, "a Mozilla file is written as UTF-8, not \\uXXXX"
    # a plain Java ASCII file keeps native2ascii escapes
    junits = f.parse(JAVA.encode()).units
    j = [u.text for u in junits].index("Goodbye")
    jout = f.build(JAVA.encode(), {str(j): "Auf Wiedersehen, grüß"})
    assert b"\\u00FC" in jout or b"\\u00fc" in jout, jout
    print("  [mozprops] Batch 1 filter writes Mozilla files as UTF-8 OK")


def main():
    test_detection_rule()
    test_output_mode()
    test_with_batch1_filter()
    print("MOZILLA PROPERTIES OK")


if __name__ == "__main__":
    main()
