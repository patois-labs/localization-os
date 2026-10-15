"""Issue #614 - an uploaded filename obeys the SAME rules as a rename on every surface.

Every upload door (web form, POST /api/documents, CLI upload, image upload, connector
store) funnels a machine-supplied name through content.safe_document_filename, which
sanitises where the operator's own rename (content.validate_document_filename) refuses.
The invariant locked here: whatever an upload stores is a FIXED POINT of the rename
validator (validate(stored) == stored), so no stored name could be refused by a later
rename, and none carries a path separator, NUL/control character, `:` or `"`.

Hermetic + offline. Final line: "UPLOAD FILENAME RULES 614 OK".
"""

import os
import random
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_upname614_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Customer, Document
from app.services import apikeys, content, security
from app.services.content_part2 import safe_document_filename, validate_document_filename

PW = "upname614-pw-123456"

NAMES = [
    "a/../../b.txt",
    "x\x00.txt",
    "..\\..\\evil.txt",
    "C:\\fakepath\\ok.txt",
    'q".txt',
    "con:x.txt",
    "~root.txt",
    "a\u2029.txt\u2029",
    "\xa0\x85\x1f\x7f\x85\u2029%\u2029\u2029.\u2029.",
]


def _assert_rename_clean(stored: str, raw: str):
    assert validate_document_filename(stored) == stored, (raw, stored)
    for bad in ("/", "\\", "\x00", ":", '"'):
        assert bad not in stored, (raw, stored)
    # Review gate: never empty, never a hidden dotfile, never a Windows device name.
    assert stored and not stored.startswith("."), (raw, stored)
    assert stored.split(".", 1)[0].rstrip(" ").upper() not in _DEVICES, (raw, stored)


_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"} | {
    f"{p}{d}" for p in ("COM", "LPT") for d in "0123456789\u00b9\u00b2\u00b3"
}

DEVICE_AND_DOT_NAMES = [
    "CON.txt",
    "con",
    "nul.tar.gz",
    "Aux .md",
    "COM1.docx",
    "lpt\u00b9.txt",
    "x/../PRN.txt",
    "C:\\fakepath\\CONOUT$.txt",
    ".htaccess",
    ".hidden.txt",
    "..x.txt",
    " .bashrc",
    "CON" + " " * 300 + ".txt",
    "." + "a" * 400 + ".txt",
]


def test_device_names_and_dotfiles_neutralised():
    from app.services.content_part2 import safe_output_name

    for raw in DEVICE_AND_DOT_NAMES:
        stored = safe_document_filename(raw)
        _assert_rename_clean(stored, raw)
        # the DEFAULT export name composed from a stored (or legacy renamed) name
        for src in (raw, stored):
            out = safe_output_name(src, "fr-FR", "docx")
            assert not out.startswith("."), (src, out)
            assert out.split(".", 1)[0].rstrip(" ").upper() not in _DEVICES, (src, out)
    assert safe_document_filename("CON.txt") == "_CON.txt"
    assert safe_document_filename(".hidden.txt") == "_.hidden.txt"
    assert safe_document_filename("console.txt") == "console.txt"  # not a device name
    print("  device names and leading dots are neutralised in stored and export names OK")


def test_sanitizer_is_a_fixed_point_of_the_validator():
    rnd = random.Random(614)
    alphabet = [
        "CON",
        "nul",
        "Com1",
        *list('ab./\\:"~ \x00\x01\x1f\x7f\x85\u2028\u2029\xa0;%'),
        "e",
        "日",
        "\u202e",
    ]
    cases = list(NAMES) + DEVICE_AND_DOT_NAMES
    for _ in range(4000):
        cases.append(
            "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 12)))
            + rnd.choice([".txt", ".md", ""])
        )
    for raw in cases:
        _assert_rename_clean(safe_document_filename(raw), raw)
    print("  sanitiser output always passes the rename validator unchanged OK")


def test_every_upload_door_stores_a_rename_clean_name():
    init_db()
    from app.main import app

    with Session(engine) as s:
        c = Customer(name="U614")
        s.add(c)
        s.commit()
        s.refresh(c)
        pid = content.project_create(
            s, "U614-p", source_locale="en-US", target_locales="de-DE", customer_id=str(c.id)
        ).id
    u = security.create_user("upname614@x.com", "U", "admin", PW)
    _row, raw = apikeys.create_key(u.id, "u614", scopes="api docs")
    H = {"X-Loc-Token": raw}
    client = TestClient(app)
    for name in NAMES + DEVICE_AND_DOT_NAMES:
        r = client.post(
            "/api/documents",
            headers=H,
            data={"project_id": str(pid)},
            files={
                "file": (
                    name + ".txt" if not name.endswith(".txt") else name,
                    b"hi there",
                    "text/plain",
                )
            },
        )
        assert r.status_code in (200, 422), (name, r.status_code, r.text[:200])
        r = client.post(
            f"/projects/{pid}/documents",
            data={"mode": "segmented"},
            files={"file": ("w-" + name, b"hello there", "text/plain")},
            follow_redirects=False,
        )
        assert r.status_code in (200, 303), (name, r.status_code, r.text[:200])
    with Session(engine) as s:
        stored = [d.filename for d in s.exec(select(Document)).all()]
    assert stored, "no document was stored"
    for fn in stored:
        _assert_rename_clean(fn, fn)
    print(f"  web + /api uploads of {len(stored)} hostile names store only rename-clean names OK")


def main():
    test_sanitizer_is_a_fixed_point_of_the_validator()
    test_device_names_and_dotfiles_neutralised()
    test_every_upload_door_stores_a_rename_clean_name()
    print("UPLOAD FILENAME RULES 614 OK")


if __name__ == "__main__":
    main()
