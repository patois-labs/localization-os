"""Issue #604 - TBX download of an EMPTY legacy project glossary was a 500.

The per-project export (web /projects/{id}/glossary/export, /api/projects/{pid}/glossary/
export, CLI `glossary export x.tbx`) now returns a valid empty TBX-Core file, like the
header-only CSV. The module-glossary concept formats keep their 422 refusal.

Hermetic + offline. Final line: "GLOSSARY EMPTY TBX 604 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_emptytbx604_")

from fastapi.testclient import TestClient
from lxml import etree
from sqlmodel import Session

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import Customer
from app.services import apikeys, content, glossary_tbx, security

PW = "emptytbx604-pw-123456"


def _assert_empty_tbx(data: bytes):
    root = etree.fromstring(data)
    assert etree.QName(root).localname == "tbx", root.tag
    assert not root.xpath("//*[local-name()='conceptEntry']"), "must have no concepts"


def main():
    init_db()
    from app.main import app

    with Session(engine) as s:
        c = Customer(name="E604")
        s.add(c)
        s.commit()
        s.refresh(c)
        p = content.project_create(
            s, "E604-proj", source_locale="en-US", target_locales="de-DE", customer_id=str(c.id)
        )
        pid = p.id
    u = security.create_user("emptytbx604@x.com", "E", "admin", PW)
    _row, raw = apikeys.create_key(u.id, "e604", scopes="api docs")
    H = {"X-Loc-Token": raw}
    client = TestClient(app)

    r = client.get(f"/projects/{pid}/glossary/export?format=tbx")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    _assert_empty_tbx(r.content)

    r = client.get(f"/api/projects/{pid}/glossary/export?format=tbx", headers=H)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    _assert_empty_tbx(r.content)

    out = os.path.join(_TMP, "empty.tbx")
    rc = cli_main(["glossary", "export", str(pid), out])
    assert rc == 0 and os.path.exists(out), rc
    with open(out, "rb") as fh:
        _assert_empty_tbx(fh.read())

    # direct service seam
    with Session(engine) as s:
        _assert_empty_tbx(glossary_tbx.export_tbx(s, pid, None))
    print(
        "  empty legacy project glossary: web, /api, CLI and service all return valid empty TBX OK"
    )
    print("GLOSSARY EMPTY TBX 604 OK")


if __name__ == "__main__":
    main()
