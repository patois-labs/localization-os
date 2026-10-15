"""#761: glossary term extraction with no API key on the default engine is a clear 4xx, not a 500.

The default engine is a built-in cloud provider ("claude") with no key and the offline mock
fallback off (the configured-install posture): get_provider raises, and extraction used to
bubble that up as HTTP 500 on the web page and the API. Now:
  service  term_extraction.extract raises ExtractionEngineUnavailable (a TermExtractionError)
  API/MCP  POST /api/glossaries/{id}/extract -> 400 {ok: false, code: engine_unavailable, error}
           (the MCP extract_terms tool relays that error text)
  web      POST /glossaries/{id}/extract -> a redirect carrying the engine_unavailable notice
  CLI      glossary module extract -> exit 1 with the message, no traceback
No candidates are written. Hermetic. Final line "TERM EXTRACTION NO KEY OK".
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_texnokey_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app import config
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import Customer, Document, Project
from app.services import apikeys, security, settings
from app.services import glossary_module as gm
from app.services import term_extraction as tex
from tests._csrf import csrf_for

init_db()
settings.set_many({"security_auth_required": "0"})
settings.invalidate_cache()
config.DEFAULT_PROVIDER = "claude"
config.MOCK_FALLBACK_OK = False
_TEXT = "Acme Rocket connects to the Widget API. The Widget API powers Rocket."


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _setup():
    with Session(engine) as s:
        c = Customer(name="TX NoKey")
        s.add(c)
        s.commit()
        s.refresh(c)
        p = Project(name="TX NK P", customer_id=c.id, source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(
            project_id=p.id, filename="doc.txt", fmt="txt", skeleton={"units": [{"text": _TEXT}]}
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        g = gm.create(s, "TX NK GL", customer_id=c.id, require_customer=True)
        return g.id, d.id


def main():
    gid, did = _setup()
    with Session(engine) as s:
        try:
            tex.extract(s, gm.get(s, gid), "fr-FR", document=s.get(Document, did))
        except tex.ExtractionEngineUnavailable as exc:
            if "API key" not in str(exc) and "key" not in str(exc):
                _fail(f"the message must name the missing key: {exc}")
        else:
            _fail("extract must raise ExtractionEngineUnavailable with no key")
        if tex.list_candidates(s, gid):
            _fail("no candidates may be written")

    c = TestClient(app, raise_server_exceptions=False)
    adm = security.create_user("texnokey@x.com", "Adm", "admin", "tx-pw-123456")
    _row, key = apikeys.create_key(adm.id, "tx", scopes="api docs")
    r = c.post(
        f"/api/glossaries/{gid}/extract",
        headers={"X-Loc-Token": key},
        json={"document_id": did, "target": "fr-FR"},
    )
    if r.status_code != 400:
        _fail(f"API must answer 400, got {r.status_code} {r.text[:200]}")
    body = r.json()
    if body.get("ok") is not False or body.get("code") != "engine_unavailable":
        _fail(f"API body: {body}")
    if "key" not in body.get("error", ""):
        _fail(f"API error must be actionable: {body}")

    csrf_for(c)
    r = c.post(
        f"/glossaries/{gid}/extract",
        data={"document_id": did, "target_locale": "fr-FR", "source_locale": "en-US"},
        follow_redirects=False,
    )
    if r.status_code not in (302, 303) or "engine_unavailable" not in r.headers.get("location", ""):
        _fail(f"web must redirect with the notice: {r.status_code} {r.headers.get('location')}")
    page = c.get(r.headers["location"])
    if page.status_code != 200 or "needs an AI engine with an API key" not in page.text:
        _fail(f"the glossary page must show the notice: {page.status_code}")

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(["glossary", "module", "extract", str(gid), str(did), "--locale", "fr-FR"])
    text = out.getvalue() + err.getvalue()
    if rc != 1 or "key" not in text or "Traceback" in text:
        _fail(f"CLI must exit 1 with the message: rc={rc} {text[:300]}")
    print("TERM EXTRACTION NO KEY OK")


if __name__ == "__main__":
    main()
