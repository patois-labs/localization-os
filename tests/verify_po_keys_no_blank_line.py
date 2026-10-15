"""GitHub #507 follow-up: the PO KEY parser (key_files_po_parse_render*) on a catalog whose
entries have no blank line between them (valid for GNU gettext).

Babel parses the entries themselves and already handles that layout, so every key is imported.
What was wrong is the separate raw-text scan that reports msgstr slots the header has no room
for: it split entries on blank lines only, so two entries in one block made it name the FIRST
entry for a dropped slot that belonged to the second. This pins both halves:

  entries   every entry is imported (names, contexts, `#.` descriptions, plural forms).
  notes     a dropped msgstr[N] is reported against the entry that carries it, with and
            without blank lines between entries; the blank-line layout is unchanged.
  splitter  _po_entry_blocks keeps msgid_plural and continuation strings inside their entry
            and splits only at a comment, msgctxt or msgid that follows a completed msgstr.

Final line: "PO KEYS NO BLANK LINE OK".
"""

import contextlib
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
for _s in (sys.stdout, sys.stderr):
    try:  # noqa: SIM105
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
from tests._bootstrap import init_env

init_env("loctms_po_keys_noblank_")

from app.db import init_db
from app.services import key_files as kf
from app.services.key_files_po_parse_render_leaf import _po_entry_blocks

init_db()
FAILS = []


def check(cond, label):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


HEADER = (
    b'msgid ""\nmsgstr ""\n"Language: ru\\n"\n"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
)
ENTRY_A = b'#. about apple\nmsgid "apple"\nmsgstr "a"\n'
ENTRY_B = (
    b'#. about files\nmsgctxt "ui"\nmsgid "file"\nmsgid_plural "files"\n'
    b'msgstr[0] "f1"\nmsgstr[1] "f2"\nmsgstr[2] "f3"\n'
)
ENTRY_C = b'msgid "cherry"\nmsgstr "c"\n'


def parse(body, **kw):
    notes = []
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        recs = kf.parse_po_keys(HEADER + body, locale="ru-RU", notes=notes, **kw)
    return recs, notes


for label, sep in (("blank lines", b"\n"), ("no blank lines", b"")):
    recs, notes = parse(sep.join([ENTRY_A, ENTRY_B, ENTRY_C]))
    names = [(r["name"], r["context"]) for r in recs]
    check(
        names == [("apple", ""), ("file", "ui"), ("cherry", "")],
        f"{label}: every entry imported {names}",
    )
    check(recs[0]["description"] == "about apple", f"{label}: the comment stays with its own entry")
    check(
        recs[1]["description"] == "about files",
        f"{label}: the second comment stays with its own entry",
    )
    dropped = [n for n in notes if "dropped" in n]
    check(
        len(dropped) == 1 and dropped[0].startswith("file:") and "msgstr[2]" in dropped[0],
        f"{label}: the dropped slot is reported against 'file' ({dropped})",
    )

# The slot that overflows belongs to the FIRST entry here: it must not be blamed on the second.
_, notes = parse(ENTRY_B + ENTRY_C)
dropped = [n for n in notes if "dropped" in n]
check(
    len(dropped) == 1 and dropped[0].startswith("file:"), f"overflow in the first entry ({dropped})"
)

# ---- splitter --------------------------------------------------------------------------
blocks = _po_entry_blocks(
    'msgid "a"\nmsgstr "x"\nmsgid "b"\nmsgid_plural "bs"\nmsgstr[0] "y"\nmsgstr[1] "z"\n'
    '# note\nmsgctxt "c"\nmsgid "c"\nmsgstr ""\n"continued"\n\nmsgid "d"\nmsgstr "w"\n'
)
check(len(blocks) == 4, f"splitter: four entries (got {len(blocks)})")
check(
    "msgid_plural" in blocks[1] and "msgstr[1]" in blocks[1], "splitter: a plural entry stays whole"
)
check(
    blocks[2].startswith("# note") and '"continued"' in blocks[2],
    "splitter: comment + ctxt + continuation stay whole",
)
check(_po_entry_blocks("") == [], "splitter: empty text -> no blocks")

if FAILS:
    print("FAILED:", len(FAILS))
    sys.exit(1)
print("PO KEYS NO BLANK LINE OK")
