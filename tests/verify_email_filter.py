"""Email filter (app/filters/email_filter.py): .eml Subject + text/plain + text/html parts,
byte-faithful splice, charset / transfer-encoding re-encoding, refusals and caps.

Hand-written fixtures only. Hermetic + offline. Final line: "EMAIL FILTER OK".
"""

import base64
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_email_")

from email import policy
from email.parser import BytesParser

from app import config
from app.filters import email_filter, get_filter, supported_exts
from app.filters.base import EmptyDocumentError, InputLimitError

F = get_filter("eml")

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(range(200))).decode()

# multipart/mixed > multipart/related > multipart/alternative (plain QP latin-1, html)
#                 + inline image (Content-ID) + a PDF attachment + a text attachment
MIXED = (
    "Return-Path: <ann@example.com>\r\n"
    "Received: from mx.example.com (mx.example.com [192.0.2.1])\r\n"
    "\tby mail.example.org; Sat, 10 Oct 2026 20:00:00 -0600\r\n"
    "From: Ann <ann@example.com>\r\n"
    "To: Bob <bob@example.org>\r\n"
    "Subject: =?ISO-8859-1?Q?Caf=E9_menu?= for {0} guests\r\n"
    "Date: Sat, 10 Oct 2026 20:00:00 -0600\r\n"
    "Message-ID: <abc123@example.com>\r\n"
    "Content-Language: en-US\r\n"
    "MIME-Version: 1.0\r\n"
    'Content-Type: multipart/mixed; boundary="==outer=="\r\n'
    "\r\n"
    "This is a multi-part message in MIME format.\r\n"
    "--==outer==\r\n"
    'Content-Type: multipart/related; boundary="rel"\r\n'
    "\r\n"
    "--rel\r\n"
    "Content-Type: multipart/alternative; boundary=alt\r\n"
    "\r\n"
    "--alt\r\n"
    'Content-Type: text/plain; charset="iso-8859-1"\r\n'
    "Content-Transfer-Encoding: quoted-printable\r\n"
    "\r\n"
    "Hello Bob,\r\n"
    "\r\n"
    "The caf=E9 opens at 9. Please confirm by replying to this message before the =\r\n"
    "end of the week so we can book a table for everyone.\r\n"
    "\r\n"
    "> Earlier: are we still on?\r\n"
    "-- \r\n"
    "Ann\r\n"
    "--alt\r\n"
    "Content-Type: text/html; charset=utf-8\r\n"
    "Content-Transfer-Encoding: base64\r\n"
    "\r\n"
    + base64.encodebytes(
        (
            '<html><head><meta charset="utf-8"></head><body>'
            "<p>Hello <b>Bob</b>,</p><p>The café opens at 9.</p>"
            '<img src="cid:logo@example.com" alt="Company logo">'
            '<a href="https://tracker.example.com/x">Open</a></body></html>'
        ).encode()
    )
    .decode()
    .replace("\n", "\r\n")
    + "--alt--\r\n"
    "\r\n"
    "--rel\r\n"
    "Content-Type: image/png\r\n"
    "Content-Transfer-Encoding: base64\r\n"
    "Content-ID: <logo@example.com>\r\n"
    'Content-Disposition: inline; filename="logo.png"\r\n'
    "\r\n" + PNG + "\r\n"
    "--rel--\r\n"
    "--==outer==\r\n"
    'Content-Type: text/plain; charset=us-ascii; name="notes.txt"\r\n'
    'Content-Disposition: attachment; filename="notes.txt"\r\n'
    "\r\n"
    "Attachment text is never translated.\r\n"
    "--==outer==\r\n"
    'Content-Type: application/pdf; name="menu.pdf"\r\n'
    "Content-Transfer-Encoding: base64\r\n"
    'Content-Disposition: attachment; filename="menu.pdf"\r\n'
    "\r\n"
    "JVBERi0xLjQKJcOkw7zDtsOfCg==\r\n"
    "--==outer==--\r\n"
    "Epilogue stays.\r\n"
).encode("ascii")

SIMPLE = (
    b"From: a@example.com\n"
    b"To: b@example.com\n"
    b"Subject: Weekly update\n"
    b"MIME-Version: 1.0\n"
    b"Content-Type: text/plain; charset=us-ascii\n"
    b"Content-Transfer-Encoding: 7bit\n"
    b"\n"
    b"Hi team,\n"
    b"\n"
    b"Your order %1$s ships on {date}.\n"
    b"  Indented line stays indented.\n"
    b"- first item\n"
    b"- second item\n"
)

FLOWED = (
    b"From: a@example.com\r\n"
    b"Subject: Flowed\r\n"
    b"Content-Type: text/plain; charset=utf-8; format=flowed\r\n"
    b"Content-Transfer-Encoding: 8bit\r\n"
    b"\r\n"
    b"This is one long logical line that a mail client wrapped with soft breaks \r\n"
    b"so it fits in seventy two columns.\r\n"
    b"\r\n"
    b">Quoted flowed text \r\n"
    b">continues here.\r\n"
)


def _texts(data: bytes):
    return [u.text for u in F.parse(data).units]


def _refused(data: bytes, exc=ValueError, needle: str = "") -> str:
    try:
        F.parse(data)
    except exc as e:
        assert needle.lower() in str(e).lower(), (needle, str(e))
        return str(e)
    raise AssertionError(f"expected refusal for {data[:60]!r}")


def _msg(data: bytes):
    return BytesParser(policy=policy.default).parsebytes(data)


def _body(msg, ctype: str) -> str:
    for part in msg.walk():
        if part.get_content_type() == ctype and part.get_content_disposition() != "attachment":
            return part.get_content()
    raise AssertionError(ctype)


def test_registration():
    assert "eml" in supported_exts()
    assert get_filter("eml").fmt == "eml"
    from app.services.connectors import msgraph
    from app.services.mime import MIME_BY_FMT

    assert msgraph.FORMATS.get("eml") == "eml"
    assert MIME_BY_FMT.get("eml") == "message/rfc822"
    with open(os.path.join(_ROOT, "app", "templates", "project.html"), encoding="utf-8") as fh:
        assert ".eml," in fh.read()
    print("  [reg] eml registered, upload accept list, msgraph and MIME maps OK")


def test_mixed_units_and_identity():
    texts = _texts(MIXED)
    assert texts == [
        "Café menu for {0} guests",
        "Hello Bob,",
        "The café opens at 9. Please confirm by replying to this message before the "
        "end of the week so we can book a table for everyone.",
        "Ann",
        "Hello ⟦g1⟧Bob⟦/g1⟧,",
        "The café opens at 9.",
        "⟦x1⟧⟦g2⟧Open⟦/g2⟧",
        "Company logo",
    ], texts
    sk = F.parse(MIXED).skeleton
    assert [p["type"] for p in sk["parts"]] == ["text/plain", "text/html"], sk
    assert F.build(MIXED, {}) == MIXED
    assert F.build(MIXED, {str(i): t for i, t in enumerate(texts)}) == MIXED
    print("  [mixed] subject + plain + html units, attachments skipped, identity OK")


def test_mixed_translated():
    out = F.build(
        MIXED,
        {
            "0": "Menü des Cafés für {0} Gäste",
            "1": "Hallo Bob,",
            "2": "Das Café öffnet um 9. Bitte bestätigen Sie bis zum Ende der Woche, "
            "damit wir einen Tisch für alle reservieren können.",
            "3": "Ann",
            "4": "Hallo ⟦g1⟧Bob⟦/g1⟧,",
            "5": "Das Café öffnet um 9 → heute.",
            "7": "Firmenlogo",
        },
    )
    msg = _msg(out)
    assert str(msg["Subject"]) == "Menü des Cafés für {0} Gäste"
    assert b"Subject: =?iso-8859-1?" in out, out[:600]  # original charset kept
    plain = _body(msg, "text/plain")
    assert "Das Café öffnet um 9. Bitte" in plain and "> Earlier: are we still on?" in plain
    assert plain.splitlines()[-2:] == ["-- ", "Ann"], plain  # signature separator kept
    html = _body(msg, "text/html")
    assert "Hallo <b>Bob</b>," in html and "9 → heute" in html, html
    assert 'src="cid:logo@example.com"' in html and 'alt="Firmenlogo"' in html, html
    assert 'href="https://tracker.example.com/x"' in html
    # plain part: latin-1 + QP kept; html part: base64 kept
    i = out.index(b"--alt\r\n")
    assert (
        b'charset="iso-8859-1"\r\nContent-Transfer-Encoding: quoted-printable' in out[i : i + 200]
    )
    assert b"Content-Transfer-Encoding: base64\r\n" in out
    # everything outside the changed ranges is byte-identical
    for chunk in (
        MIXED[: MIXED.index(b"Subject:")],
        MIXED[MIXED.index(b"Date:") : MIXED.index(b"Content-Language:")],
        MIXED[MIXED.index(b"--rel\r\nContent-Type: image/png") :],
    ):
        assert chunk in out, chunk[:80]
    imgs = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert imgs and imgs[0]["Content-ID"] == "<logo@example.com>"
    assert imgs[0].get_content() == base64.b64decode(PNG)
    print("  [mixed] translated subject/plain/html, charsets + CTEs + attachments intact OK")


def test_charset_fallback_to_utf8():
    out = F.build(MIXED, {"1": "Привет, Боб!"})  # not representable in latin-1
    msg = _msg(out)
    plain = next(p for p in msg.walk() if p.get_content_type() == "text/plain")
    assert plain.get_content_charset() == "utf-8", plain["Content-Type"]
    assert "Привет, Боб!" in plain.get_content()
    assert plain["Content-Transfer-Encoding"] == "quoted-printable"
    print("  [enc] latin-1 part falls back to UTF-8 with the header rewritten OK")


def test_simple_7bit():
    texts = _texts(SIMPLE)
    assert texts == [
        "Weekly update",
        "Hi team,",
        "Your order %1$s ships on {date}.",
        "Indented line stays indented.",
        "- first item",
        "- second item",
    ], texts
    assert F.build(SIMPLE, {}) == SIMPLE
    out = F.build(SIMPLE, {"2": "Ihre Bestellung %1$s wird am {date} versandt.", "3": "Bleibt."})
    assert out == SIMPLE.replace(
        b"Your order %1$s ships on {date}.", b"Ihre Bestellung %1$s wird am {date} versandt."
    ).replace(b"  Indented line stays indented.", b"  Bleibt."), out
    # non-ASCII into a 7bit us-ascii part: charset and CTE both change, recorded in headers
    out = F.build(SIMPLE, {"1": "Hallo Größe,"})
    msg = _msg(out)
    assert (
        msg.get_content_charset() == "utf-8"
        and msg["Content-Transfer-Encoding"] == "quoted-printable"
    )
    assert msg.get_content().startswith("Hallo Größe,\n"), msg.get_content()
    assert out.startswith(SIMPLE[: SIMPLE.index(b"Content-Type")])
    print("  [plain] 7bit lines, placeholders, indentation, list items, 7bit->QP fallback OK")


def test_flowed():
    texts = _texts(FLOWED)
    assert texts == [
        "Flowed",
        "This is one long logical line that a mail client wrapped with soft breaks "
        "so it fits in seventy two columns.",
    ], texts
    assert F.build(FLOWED, {}) == FLOWED
    new = "Dies ist eine lange logische Zeile, die ein Mailprogramm mit weichen Umbruechen " * 2
    out = F.build(FLOWED, {"1": new.strip()})
    body = out.split(b"\r\n\r\n", 1)[1]
    lines = body.split(b"\r\n")
    assert all(len(ln) <= 78 for ln in lines), lines
    assert lines[0].endswith(b" ") and not lines[2].endswith(b" "), lines
    # unwrapping the flowed lines gives the translation back
    first_para = body.split(b"\r\n\r\n")[0].decode()
    assert first_para.replace(" \r\n", " ") == new.strip(), first_para
    assert b">Quoted flowed text \r\n>continues here.\r\n" in out
    # a flowed line directly followed by a quote or the signature ends its paragraph
    edge = (
        b"Subject: e\nContent-Type: text/plain; format=flowed\n\n"
        b"Mine flows \n>Theirs\n"
        b"Sign off \n-- \nAnn\n"
    )
    assert _texts(edge) == ["e", "Mine flows", "Sign off", "Ann"], _texts(edge)
    out = F.build(edge, {"1": "Meins", "2": "Gruss"})
    assert out.endswith(b"\n\nMeins\n>Theirs\nGruss\n-- \nAnn\n"), out
    print("  [flowed] RFC 3676 unwrap, re-wrap with soft breaks, quotes kept OK")


def test_hard_wrapped_paragraph():
    data = (
        b"Subject: x\n\n"
        b"This paragraph was hard wrapped by an old mail client at sixty-eight\n"
        b"columns, so its lines belong together as one sentence for translation.\n"
    )
    texts = _texts(data)
    assert texts[1].startswith("This paragraph") and texts[1].endswith("translation."), texts
    out = F.build(data, {"1": "Ein Absatz " * 20})
    body = out.split(b"\n\n", 1)[1].decode()
    assert all(len(ln) <= 71 for ln in body.splitlines()), body
    assert " ".join(body.split()) == ("Ein Absatz " * 20).strip()
    print("  [plain] hard-wrapped paragraph joined and re-wrapped at its width OK")


def test_localized_content_language():
    out = F.build_localized(MIXED, {"1": "Hallo Bob,"}, "de-DE")
    assert b"Content-Language: de-DE\r\n" in out, out[:800]
    html = _body(_msg(out), "text/html")
    assert 'lang="de-DE"' in html, html
    # no Content-Language header: none is added
    out = F.build_localized(SIMPLE, {}, "de-DE")
    assert b"Content-Language" not in out and out == SIMPLE
    print("  [lang] Content-Language and html lang stamped with the target OK")


def test_subject_8bit_and_folded():
    data = (
        b"Subject: Gr\xc3\xbc\xc3\x9fe\r\n aus K\xc3\xb6ln\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
        b"K\xc3\xb6ln.\r\n"
    )
    assert _texts(data) == ["Grüße aus Köln", "Köln."], _texts(data)
    assert F.build(data, {}) == data
    out = F.build(data, {"0": "Greetings from Cologne", "1": "Köln ✓."})
    assert out.startswith(b"Subject: Greetings from Cologne\r\nContent-Type"), out
    assert out.endswith("Köln ✓.\r\n".encode()), out
    print("  [subject] RFC 6532 8-bit and folded subjects, 8bit body kept 8bit OK")


def test_refusals():
    signed = (
        b"Subject: s\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; '
        b"micalg=sha-256; boundary=b\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nHi\r\n"
        b"--b\r\nContent-Type: application/pkcs7-signature\r\n\r\nMIAG\r\n--b--\r\n"
    )
    _refused(signed, InputLimitError, "signature")
    _refused(
        b"Subject: e\r\nContent-Type: application/pkcs7-mime; smime-type=enveloped-data\r\n"
        b"\r\nMIA\r\n",
        InputLimitError,
        "S/MIME",
    )
    _refused(
        b'Subject: p\r\nContent-Type: multipart/encrypted; protocol="application/pgp-encrypted";'
        b" boundary=x\r\n\r\n--x\r\nContent-Type: application/pgp-encrypted\r\n\r\n"
        b"Version: 1\r\n--x--\r\n",
        InputLimitError,
        "encrypted",
    )
    _refused(
        b"Subject: p\n\n-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nHi\n",
        InputLimitError,
        "signature",
    )
    _refused(b"%PDF-1.4 not an email", InputLimitError, "not an email")
    _refused(b"\x00\x01binary", InputLimitError, "not an email")
    _refused(
        b"Subject: c\nContent-Type: text/plain; charset=x-made-up\n\nHi\n",
        InputLimitError,
        "charset",
    )
    _refused(
        b"Subject: c\nContent-Type: text/plain; charset=base64\n\nHi\n", InputLimitError, "charset"
    )
    _refused(
        b"Subject: c\nContent-Type: text/plain; charset=utf-8\n\n\xff\xfe bad\n",
        InputLimitError,
        "do not match",
    )
    _refused(
        b"From: a@example.com\nContent-Type: image/png\n\nxx\n", EmptyDocumentError, "no Subject"
    )
    _refused(b"Subject: h\n" + b"X-Pad: y\n" * 150000, InputLimitError, "header block")
    # 8-bit text mislabelled us-ascii is read as UTF-8 and relabelled when rebuilt
    mis = b"Subject: m\nContent-Type: text/plain; charset=us-ascii\n\nCaf\xc3\xa9\n"
    assert _texts(mis) == ["m", "Café"]
    out = F.build(mis, {"1": "Café ✓"})
    assert b'charset="utf-8"' in out and _msg(out).get_content() == "Café ✓\n", out
    print("  [refuse] S/MIME, PGP/MIME, inline PGP, not-an-email, bad charsets, empty OK")


def test_caps():
    # nesting depth
    deep = b"Subject: d\n"
    for i in range(email_filter.MAX_DEPTH + 2):
        deep += f'Content-Type: multipart/mixed; boundary="b{i}"\n\n--b{i}\n'.encode()
    deep += b"Content-Type: text/plain\n\nhi\n"
    _refused(deep, InputLimitError, "deeper")
    many = (
        b"Subject: m\nContent-Type: multipart/mixed; boundary=z\n\n"
        + b"".join(
            b"--z\nContent-Type: text/plain\n\npart\n" for _ in range(email_filter.MAX_PARTS + 1)
        )
        + b"--z--\n"
    )
    _refused(many, InputLimitError, "MIME parts")
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        lots = b"Subject: s\n\n" + b"".join(b"line %d\n\n" % i for i in range(10))
        _refused(lots, InputLimitError, "MAX_DOCUMENT_SEGMENTS")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    old = config.MAX_SEGMENT_CHARS
    try:
        config.MAX_SEGMENT_CHARS = 1000
        _refused(b"Subject: s\n\n" + b"word " * 400 + b"\n", InputLimitError, "MAX_SEGMENT_CHARS")
    finally:
        config.MAX_SEGMENT_CHARS = old
    # a boundary that never closes still parses (last part runs to the end)
    open_ended = (
        b"Subject: o\nContent-Type: multipart/mixed; boundary=q\n\n"
        b"--q\nContent-Type: text/plain\n\nbody\n"
    )
    assert _texts(open_ended) == ["o", "body"]
    assert F.build(open_ended, {}) == open_ended
    print("  [caps] depth, part count, segment and char caps, unterminated multipart OK")


def test_message_rfc822_attachment_untouched():
    data = (
        b"Subject: Fwd: hello\n"
        b"Content-Type: multipart/mixed; boundary=f\n\n"
        b"--f\nContent-Type: text/plain\n\nSee below.\n"
        b"--f\nContent-Type: message/rfc822\n\nSubject: inner\n\nInner body.\n"
        b"--f--\n"
    )
    assert _texts(data) == ["Fwd: hello", "See below."]
    out = F.build(data, {"1": "Siehe unten."})
    assert b"Subject: inner\n\nInner body.\n" in out and b"Siehe unten." in out
    print("  [rfc822] forwarded message kept as an attachment OK")


BND_MSG = (
    b"Subject: s\nMIME-Version: 1.0\n"
    b'Content-Type: multipart/mixed; boundary="BND"\n\n'
    b"--BND\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: 8bit\n\n"
    b"Hello there.\n"
    b"--BND\nContent-Type: text/html; charset=us-ascii\n"
    b"Content-Transfer-Encoding: quoted-printable\n\n"
    b"<p>Hi</p>\n"
    b"--BND\nContent-Type: application/octet-stream\n"
    b"Content-Disposition: attachment; filename=a.bin\n\nAAAA\n--BND--\n"
)


def test_boundary_injection():
    """Gate train-11: a translation holding a line that reads as a MIME delimiter
    (`--BND`) must not end its part and forge a new one: the body goes base64."""
    f = get_filter("eml")
    evil = "Hi\n--BND\nContent-Type: text/html\n\n<b>owned</b>\n--BND--"
    out = f.build(BND_MSG, {"1": evil, "2": "Oi\n--BND--\n"})
    parts = [p for p in _msg(out).walk() if not p.is_multipart()]
    assert len(parts) == 3, [p.get_content_type() for p in parts]
    assert parts[0].get_content_type() == "text/plain"
    assert parts[0]["Content-Transfer-Encoding"] == "base64", out
    assert parts[0].get_content().startswith("Hi\n--BND\n"), parts[0].get_content()
    assert parts[1]["Content-Transfer-Encoding"] == "base64", out
    assert out.count(b"\n--BND\n") == 3 and out.endswith(b"AAAA\n--BND--\n"), out
    assert out.count(b"Content-Transfer-Encoding:") == 2  # replaced in place, not added
    texts = [u.text for u in f.parse(out).units]
    assert texts[1] == "Hi" and "<b>owned</b>" in texts and "--BND" in texts, texts
    # a harmless translation keeps the original transfer encodings
    out2 = f.build(BND_MSG, {"1": "Hallo dort.", "2": "Hallo"})
    assert b"Content-Transfer-Encoding: 8bit\n\nHallo dort.\n--BND" in out2, out2
    print("  [build] boundary-like translation lines are base64-encoded, never a new part OK")


def test_api_and_cli_round_trip():
    """Upload an .eml through the API and the CLI, mock-translate, export."""
    import re
    import tempfile

    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    loc = "de-DE"
    init_db()
    with Session(engine) as s:
        p = Project(name="email", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        pid = p.id
    user = security.create_user("email@x.test", "email", Role.ADMIN, "email-pw-123456")
    _row, raw_key = apikeys.create_key(user.id, "email", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw_key}  # pregate: allow-secret throwaway test key

    def check(out: bytes, surface: str):
        msg = _msg(out)
        assert str(msg["Subject"]).startswith(f"[{loc}]"), (surface, msg["Subject"])
        html = _body(msg, "text/html")
        assert f"[{loc}]" in html and 'src="cid:logo@example.com"' in html, (surface, html)
        assert b"Content-ID: <logo@example.com>" in out and b"Epilogue stays." in out
        assert b"Content-Language: de-DE" in out, surface

    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("menu.eml", MIXED, "message/rfc822")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc), "--locale", loc])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc}/download", headers=hdr, params={"locale": loc})
    assert r.status_code == 200, (r.status_code, r.text)
    check(r.content, "api")
    r = client.get("/api/formats", headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text)
    assert {"ext": "eml", "name": "Email message (EML)"} in r.json()["formats"], r.text
    # signed mail refused with the plain message on the API
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={
            "file": (
                "signed.eml",
                b"Subject: s\r\nContent-Type: application/pkcs7-mime\r\n\r\nMIA\r\n",
                "message/rfc822",
            )
        },
    )
    # gate: a content refusal is 422 (unreadable), not the 413 over-a-limit answer
    assert r.status_code == 422 and "S/MIME" in r.text, (r.status_code, r.text)

    tmp = tempfile.mkdtemp(prefix="loctms_email_cli_")
    src = os.path.join(tmp, "menu.eml")
    with open(src, "wb") as fh:
        fh.write(MIXED)
    code, out, err = run_cli(["doc", "upload", str(pid), src])
    assert code == 0, (code, out, err)
    m = re.search(r"Uploaded #(\d+): menu\.eml \(eml,", out)
    assert m, out
    cli_doc = int(m.group(1))
    code, out, err = run_cli(["doc", "translate", str(cli_doc), "--locale", loc])
    assert code == 0, (code, out, err)
    dest = os.path.join(tmp, "out.eml")
    code, out, err = run_cli(["doc", "export", str(cli_doc), "--locale", loc, "-o", dest])
    assert code == 0, (code, out, err)
    with open(dest, "rb") as fh:
        check(fh.read(), "cli")
    print("  [surfaces] API upload/download and CLI upload/export round trip OK")


def main():
    test_registration()
    test_mixed_units_and_identity()
    test_mixed_translated()
    test_charset_fallback_to_utf8()
    test_simple_7bit()
    test_flowed()
    test_hard_wrapped_paragraph()
    test_localized_content_language()
    test_subject_8bit_and_folded()
    test_refusals()
    test_caps()
    test_message_rfc822_attachment_untouched()
    test_boundary_injection()
    test_api_and_cli_round_trip()
    print("EMAIL FILTER OK")


if __name__ == "__main__":
    main()
