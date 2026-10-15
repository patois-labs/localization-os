"""Phase-2 hardening F3 — Content-Disposition parameter injection via a document `"`.

`content.rename_document` blocked C0/DEL control chars but PERMITTED the double-quote
`"`, which reached the download `Content-Disposition: attachment; filename="..."`
UNESCAPED (app/web/api.py, app/web/documents.py) — so a same-tenant writer could
rename a document to `a"; x=1 .txt` and break OUT of the quoted filename, injecting
header parameters (a spoofed `filename*=`, parser confusion).

Fix at BOTH seams:
  [A] rename_document REJECTS a `"` (ValueError -> 400 on /api, error redirect on web).
  [B] the download emit seam builds the header via content.content_disposition_attachment
      (RFC-6266): an ASCII `filename="..."` fallback whose value is header_safe_filename'd
      (the injected `"`/backslash/control neutralised to `_`) PLUS a
      `filename*=UTF-8''<percent-encoded>` ext-value (the `"` is `%22`, never a literal) —
      protecting rows renamed BEFORE the guard landed and the `locale`/`fmt` pieces
      interpolated alongside the stem, AND fixing the non-latin-1 filename 500 (the
      RFC-6266 header is ASCII/wire-encodable by construction).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). Final line:
"DOC CD INJECTION OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cdinj_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Project,
    Role,
    Segment,
    SegStatus,
    TargetLocale,
    TransMode,
)
from app.services import apikeys, content, security
from app.services.ingest import ingest_document

PW = "cdinj-pw-123456"
MD_BYTES = b"# Title\n\nHello world.\n\nSecond paragraph.\n"
CLIENT = TestClient(app, raise_server_exceptions=False)


def _token(email, scopes="api docs keys admin", role=Role.ADMIN):
    user = security.create_user(email, email, role, PW)
    _row, raw = apikeys.create_key(user.id, "cdinj", scopes=scopes)
    return raw


def _project_with_doc(locale="fr-FR"):
    with Session(engine) as s:
        c = Customer(name="CdInj Cust")
        s.add(c)
        s.commit()
        s.refresh(c)
        p = Project(
            name="cdinj", org_id=1, source_locale="en-US", default_provider="mock", customer_id=c.id
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=locale))
        s.commit()
        d = ingest_document(s, p, "guide.md", MD_BYTES, TransMode.SEGMENTED)
        s.commit()
        s.refresh(d)
        segs = s.exec(
            select(Segment)
            .where(Segment.document_id == d.id, Segment.target_locale == locale)
            .order_by(Segment.order_index)
        ).all()
        for seg in segs:
            seg.target_text = "traduit"
            seg.status = SegStatus.MT
            s.add(seg)
        s.commit()
        return p.id, d.id


def test_rename_rejects_quote_service_and_api():
    """[A] the guard: a `"` in a rename is a clean refusal on the service AND /api."""
    tok = _token("cdinj-rename@x.com")
    _pid, doc_id = _project_with_doc()

    # service level
    with Session(engine) as s:
        raised = False
        try:
            content.rename_document(s, doc_id, 'evil".txt', actor="t")
        except ValueError:
            raised = True
        assert raised, "rename_document must raise ValueError on a double-quote name"

    # /api level -> 400 (not 500, not a silent accept)
    for name in ('evil".txt', 'a"; x=1 .txt'):
        r = CLIENT.post(
            "/api/documents/%d/rename" % doc_id,  # noqa: UP031
            json={"filename": name},
            headers={"X-Loc-Token": tok},
        )
        assert r.status_code == 400, "a double-quote rename must 400: got %d %r" % (  # noqa: UP031
            r.status_code,
            r.text,
        )

    # a LEGITIMATE Unicode/space/`;` name still succeeds (no over-rejection)
    r = CLIENT.post(
        "/api/documents/%d/rename" % doc_id,  # noqa: UP031
        json={"filename": "naïve café; v2.md"},
        headers={"X-Loc-Token": tok},
    )
    assert r.status_code == 200, ("a legit unicode name must succeed", r.status_code, r.text)
    print(
        '  [A] rename rejects `"` (service ValueError + /api 400); a legit '
        "unicode/space/`;` name still renames OK"
    )


def test_download_header_carries_no_unescaped_quote():
    """[B] the seam: even a row whose filename ALREADY holds a `"` (renamed before the
    guard) emits an RFC-6266 Content-Disposition with the `"` NEUTRALISED — no quote
    breaks out of the ASCII `filename="..."` fallback to inject a parameter, and the
    injected `; x=1` never becomes a real top-level parameter."""
    tok = _token("cdinj-dl@x.com")
    _pid, doc_id = _project_with_doc()
    # simulate a pre-guard row: write the hostile filename straight into the DB,
    # bypassing rename_document (which would now reject it).
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        d.filename = 'a"; x=1 .md'
        s.add(d)
        s.commit()

    r = CLIENT.get(
        "/api/documents/%d/download" % doc_id,  # noqa: UP031
        params={"locale": "fr-FR"},
        headers={"X-Loc-Token": tok},
    )
    assert r.status_code == 200, ("download must succeed", r.status_code, r.text)
    cd = r.headers.get("content-disposition", "")
    # RFC-6266 shape: an ASCII `filename="..."` fallback + a `filename*=UTF-8''<pct>` value.
    cd.encode("latin-1")  # MUST be wire-encodable (the item-6 fix; a unicode name 500'd)
    assert cd.startswith('attachment; filename="'), cd
    assert "filename*=UTF-8''" in cd, cd
    # exactly the TWO wrapping quotes of the fallback — the injected `"` was NEUTRALISED
    # (-> `_`), never reflected/broken out (a reflected quote would make three). So `; x=1`
    # stays a LITERAL inside the quoted fallback (a `;` inside the quotes is inert) and no
    # real `x=1` parameter escaped.
    assert cd.count('"') == 2, f"the injected quote must be neutralised, not reflected: {cd!r}"
    # SLICE-BASED backup (non-vacuous, independent of the count): locate the two quotes and
    # require the SECOND (the fallback's close-quote) to be immediately followed by the
    # `; filename*=` parameter -- so nothing broke OUT between the fallback value and the ext
    # param. A reflected/broken-out quote would put the second quote EARLY, followed by the
    # injected `; x=1 ...` text (NOT `; filename*=`), reddening this even if the count check
    # were somehow satisfied. (`cd.split('"')[1]` alone would be tautological -- avoided.)
    first_q = cd.index('"')
    second_q = cd.index('"', first_q + 1)
    assert cd[second_q:].startswith('"; filename*='), (
        "the fallback close-quote must be immediately followed by filename*, with no injected "
        f"parameter escaping between them: {cd!r}"
    )
    inner = cd[first_q + 1 : second_q]  # the quoted fallback VALUE
    assert '"' not in inner and "; x=1" in inner, (  # the `; x=1` is a LITERAL inside the quotes
        "the injected `; x=1` must stay a literal inside the quoted fallback, no quote inside: {!r}".format(  # noqa: E501, UP032
            cd
        )
    )
    # and the `filename*` ext-value percent-encodes the injected quote (%22), never a
    # literal breakout quote — so the raw name is carried without a parameter escape.
    assert "%22" in cd, f"filename* must percent-encode the injected quote: {cd!r}"
    print(
        '  [B] a pre-guard `"`-bearing filename downloads with a clean RFC-6266 '
        'Content-Disposition - `"` neutralised in the fallback + %-encoded in '
        f"filename* ({cd!r}) OK"
    )


def test_header_safe_filename_unit():
    """The seam helper in isolation: neutralise the breakout chars, keep the rest."""
    assert '"' not in content.header_safe_filename('a"; x=1 .txt')
    assert "\\" not in content.header_safe_filename("a\\.txt")
    assert "\r" not in content.header_safe_filename("a\r\nX: y.txt")
    # legitimate characters are preserved verbatim
    assert content.header_safe_filename("naïve café; v2.docx") == "naïve café; v2.docx"
    print(
        '  [C] header_safe_filename neutralises `"`/`\\`/controls, preserves unicode/space/`;` OK'
    )


def main():
    init_db()
    test_rename_rejects_quote_service_and_api()
    test_download_header_carries_no_unescaped_quote()
    test_header_safe_filename_unit()
    print("DOC CD INJECTION OK")


if __name__ == "__main__":
    main()
