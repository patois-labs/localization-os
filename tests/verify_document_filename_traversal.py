"""Issue #142 - document filename path traversal (rename accepts `../`, CLI export
writes outside the CWD).

`content.rename_document` refused a blank name, ASCII control characters and a
double-quote, but PERMITTED `/`, `\\`, `..`, a drive letter and a UNC prefix. So a
same-tenant writer could rename a document to `../../../../Windows/win.ini.txt`, and the
CLI's DEFAULT export name (`<stem>.<locale>.<fmt>` in the CWD, composed straight from the
stored filename) then tried to WRITE there - an arbitrary write where permitted, a
PermissionError traceback where not.

Fixed at BOTH kinds of seam:
  [A] ONE validator, `content.validate_document_filename`, REFUSES an operator-supplied
      name carrying a path separator / `..` / `~` / a drive or stream colon (ValueError ->
      400 on /api, the error redirect on web, a clean CLI usage error). Blank/control/quote
      keep their existing messages.
  [B] The INGEST seams (upload on web + /api + CLI, image upload, connector-created
      documents) SANITISE instead of refusing (`content.safe_document_filename` =
      basename + the same character rules): a browser or a remote system sending a path is
      not the operator's mistake, and an upload must not fail on it.
  [C] DEFENCE IN DEPTH at every filesystem sink that composes a path from a stored
      filename (`loc-tms doc export`, `loc-tms image export/render` with no `-o`): the
      name is basenamed via `content.safe_output_stem` AND the resolved default path must
      stay inside the CWD, so a row poisoned BEFORE this fix (no migration) still cannot
      escape. An explicit `-o` is the operator's own choice and is NOT constrained.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). Final line:
"DOC FILENAME TRAVERSAL OK".
"""

import os
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_fntrav_")

from contextlib import contextmanager

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
from app.services import apikeys, content, security, settings
from app.services.ingest import ingest_document
from tests._cli_runner import run_cli

PW = "fntrav-pw-123456"
MD_BYTES = b"# Title\n\nHello world.\n\nSecond paragraph.\n"
SVG_BYTES = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="120">\n'
    b'  <text x="16" y="40">Summer Sale</text>\n'
    b'  <text x="16" y="80">Shop now</text>\n'
    b"</svg>\n"
)
LOCALE = "fr-FR"
CLIENT = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

# Every operator-supplied name the validator must REFUSE. The first six are the #142
# separator/traversal cases (RED before the fix); the last three are the pre-existing
# blank/control/quote rules, asserted here so the shared validator cannot drop them.
BAD_NAMES = [
    "../x.txt",
    "..\\x.txt",
    "a/b.txt",
    "C:\\x.txt",
    "\\\\srv\\x.txt",
    "x\x00.txt",
    "..",
    "~/x.txt",
    "   ",
    "a\r\nX-Injected: 1.txt",
    'evil".txt',
]


def _drop_link(path):
    """Remove a symlink OR a directory junction ENTRY without following it."""
    for remove in (os.remove, os.rmdir):
        try:
            remove(path)
            return True
        except OSError:
            continue
    return False


def _make_escape_link(link_path, file_target, dir_target):
    """Make `link_path` resolve OUTSIDE the CWD, by whatever means this OS allows.

    A file symlink needs SeCreateSymbolicLink (admin or developer mode) on Windows; a
    DIRECTORY JUNCTION does not, so it is the fallback -- either way the single-component
    default name resolves outside the CWD, which is exactly what the containment check
    must catch. Returns False when neither is permitted (the test then skips)."""
    try:
        os.symlink(file_target, link_path)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name != "nt":
        return False
    try:
        rc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link_path, dir_target],
            capture_output=True,
            timeout=30,
        ).returncode
        return rc == 0 and os.path.exists(link_path)
    except (OSError, subprocess.SubprocessError):
        return False


@contextmanager
def _in_temp_cwd():
    """Run a block with the process CWD inside a throwaway directory, so a CLI default
    output name is written (or refused) somewhere disposable."""
    prev = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="loctms_fntrav_cwd_")
    os.chdir(tmp)
    try:
        yield tmp
    finally:
        os.chdir(prev)
        # REAP IT. The harness points TMP inside the clone, and a left-behind export (or,
        # worse, the junction the containment case creates) makes the repo-wide ruff
        # ratchet fail with an "Access is denied" read error on a stray file.
        shutil.rmtree(tmp, ignore_errors=True)  # never FOLLOWS a link/junction


def _token(email, scopes="api docs keys admin", role=Role.ADMIN):
    user = security.create_user(email, email, role, PW)
    _row, raw = apikeys.create_key(user.id, "fntrav", scopes=scopes)
    return raw


def _project(name="fntrav"):
    with Session(engine) as s:
        c = Customer(name="FnTrav Cust " + name)
        s.add(c)
        s.commit()
        s.refresh(c)
        p = Project(
            name=name, org_id=1, source_locale="en-US", default_provider="mock", customer_id=c.id
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOCALE))
        s.commit()
        return p.id


def _doc(project_id, filename="guide.md", data=MD_BYTES, translate=True):
    with Session(engine) as s:
        p = s.get(Project, project_id)
        d = ingest_document(s, p, filename, data, TransMode.SEGMENTED)
        s.commit()
        s.refresh(d)
        if translate:
            segs = s.exec(
                select(Segment)
                .where(Segment.document_id == d.id, Segment.target_locale == LOCALE)
                .order_by(Segment.order_index)
            ).all()
            for seg in segs:
                seg.target_text = "traduit"
                seg.status = SegStatus.MT
                s.add(seg)
            s.commit()
        return d.id


def _poison(doc_id, name):
    """Write a hostile filename STRAIGHT into the DB, bypassing the validator - the
    simulation of a row renamed BEFORE the fix landed (there is no migration)."""
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        d.filename = name
        s.add(d)
        s.commit()


def _filename(doc_id):
    with Session(engine) as s:
        return s.get(Document, doc_id).filename


# --- (a) the validator, at the service ---------------------------------------
def test_rename_refuses_traversal_names():
    pid = _project("svc")
    doc_id = _doc(pid)
    for name in BAD_NAMES:
        with Session(engine) as s:
            raised = ""
            try:
                content.rename_document(s, doc_id, name, actor="t")
            except ValueError as exc:
                raised = str(exc)
            assert raised, f"rename_document must refuse {name!r}"
            assert raised.startswith("document filename "), (name, raised)
            assert "\n" not in raised and "Traceback" not in raised, (name, raised)
        assert _filename(doc_id) == "guide.md", ("the stored name must not change", name)

    # ...and a perfectly normal name still renames (no over-rejection).
    with Session(engine) as s:
        content.rename_document(s, doc_id, "report (final).txt", actor="t")
    assert _filename(doc_id) == "report (final).txt"
    with Session(engine) as s:
        content.rename_document(s, doc_id, "naïve café; v2.md", actor="t")
    assert _filename(doc_id) == "naïve café; v2.md"
    print(
        "  [a] rename_document refuses separators/`..`/drive/UNC/`~` + the old rules; "
        "a normal name (spaces, parens, unicode, `;`) still renames OK"
    )


# --- (b) both rename surfaces ------------------------------------------------
def test_api_and_web_rename_refuse_traversal():
    tok = _token("fntrav-rename@x.com")
    pid = _project("surfaces")
    doc_id = _doc(pid)
    evil = "../../../../Windows/win.ini.txt"

    r = CLIENT.post(
        f"/api/documents/{doc_id}/rename",
        json={"filename": evil},
        headers={"X-Loc-Token": tok},
    )
    assert r.status_code == 400, (r.status_code, r.text[:200])
    body = r.json()
    assert body.get("ok") is False and isinstance(body.get("error"), str), body
    assert "path separator" in body["error"], body
    assert _filename(doc_id) == "guide.md", "the /api refusal must not have renamed the row"

    # the web twin: a 303 back to the document with the SAME message in ?err= (the web
    # surface has always redirected with an error, it does not render a 400 body).
    r = CLIENT.post(f"/documents/{doc_id}/rename", data={"filename": evil})
    assert r.status_code == 303, (r.status_code, r.text[:200])
    loc = r.headers.get("location", "")
    assert "err=" in loc and "separator" in loc.replace("+", " ").replace("%20", " "), loc
    assert _filename(doc_id) == "guide.md", "the web refusal must not have renamed the row"

    # CLI parity: a clean usage error, never a traceback.
    code, out, err = run_cli(["doc", "rename", str(doc_id), evil])
    assert code != 0, (code, out, err)
    assert "Traceback" not in (out + err), (out, err)
    assert "path separator" in (out + err), (out, err)
    assert _filename(doc_id) == "guide.md"
    print(
        "  [b] /api rename -> 400 {ok:false,error}, web -> 303 ?err=, CLI -> clean "
        "usage error; the stored filename is unchanged on all three OK"
    )


# --- (c) the ingest seams SANITISE ------------------------------------------
def test_upload_sanitises_traversal_filename():
    tok = _token("fntrav-upload@x.com")
    pid = _project("upload")

    # the service seam every upload surface funnels through
    with Session(engine) as s:
        p = s.get(Project, pid)
        d = ingest_document(s, p, "../../evil.md", MD_BYTES, TransMode.SEGMENTED)
        s.commit()
        assert d.filename == "evil.md", d.filename
        d2 = ingest_document(s, p, "C:\\Windows\\win.ini.md", MD_BYTES, TransMode.SEGMENTED)
        s.commit()
        assert d2.filename == "win.ini.md", d2.filename

    # ...and over the wire (multipart filename), where a browser/CLI client - not the
    # operator - chose the name, so it is CLEANED, never refused.
    r = CLIENT.post(
        "/api/documents",
        files={"file": ("../../wire.md", MD_BYTES, "text/markdown")},
        data={"project_id": str(pid)},
        headers={"X-Loc-Token": tok},
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])
    new_id = r.json()["document_id"]
    assert _filename(new_id) == "wire.md", _filename(new_id)

    # the connector ingest seam (a REMOTE system's name; sanitised for the same reason)
    from app.services.connectors import store as cstore

    cfg = {"base_id": "app1", "table": "t"}
    cred = {"api_key": "pat"}  # pregate: allow-secret placeholder connector credential
    conn = cstore.create("airtable", "FnTrav base", cfg, cred, actor="fntrav")
    with Session(engine) as s:
        from app.models import Connection

        row = s.get(Connection, conn.id)
        cdoc = cstore._new_connector_doc(
            s, row, [{"text": "hello"}], "../../remote.json", "fntrav", locales=[LOCALE]
        )
        assert cdoc.filename == "remote.json", cdoc.filename
    print(
        "  [c] upload/ingest seams basename a traversal filename (service, /api "
        "multipart, connector) instead of failing the upload OK"
    )


# --- (d) the CLI export sink -------------------------------------------------
def test_cli_doc_export_default_out_cannot_escape_cwd():
    pid = _project("cliexport")
    doc_id = _doc(pid)
    _poison(doc_id, "../../../../Windows/win.ini.txt")

    with _in_temp_cwd() as tmp:
        code, out, err = run_cli(["doc", "export", str(doc_id), "--locale", LOCALE])
        assert "Traceback" not in (out + err), (out, err)
        assert code == 0, ("a poisoned row must still export into the CWD", code, out, err)
        produced = sorted(os.listdir(tmp))
        assert produced == [f"win.ini.{LOCALE}.md"], produced
        parent = os.path.dirname(tmp)
        assert not os.path.exists(os.path.join(parent, f"win.ini.{LOCALE}.md")), (
            "nothing may be written outside the CWD"
        )

    # an EXPLICIT -o outside the CWD is the operator's own choice and is honoured.
    with _in_temp_cwd() as tmp:
        outside_dir = tempfile.mkdtemp(prefix="loctms_fntrav_out_")
        outside = os.path.join(outside_dir, "chosen.md")
        code, out, err = run_cli(["doc", "export", str(doc_id), "--locale", LOCALE, "-o", outside])
        assert code == 0, (code, out, err)
        assert os.path.isfile(outside), outside
        assert os.listdir(tmp) == [], os.listdir(tmp)
        shutil.rmtree(outside_dir, ignore_errors=True)  # TMP is inside the clone here
    print(
        "  [d] `doc export` with no -o writes the BASENAMED default inside the CWD "
        "(poisoned row, no traceback); an explicit -o outside the CWD is honoured OK"
    )


# --- (e) the other sink: the image export/render default out -----------------
def test_cli_image_export_default_out_cannot_escape_cwd():
    settings.set_many({"image_localization_enabled": "1"})
    pid = _project("cliimage")
    from app.services import image_localization as il

    with Session(engine) as s:
        p = s.get(Project, pid)
        doc = il.ingest_image(s, p, "banner.svg", SVG_BYTES, actor="fntrav")
        s.commit()
        il.translate_image(s, doc, LOCALE, actor="fntrav")
        s.commit()
        doc_id = doc.id
    _poison(doc_id, "../../../../Windows/evil.svg")

    with _in_temp_cwd() as tmp:
        code, out, err = run_cli(["image", "export", str(doc_id), "--locale", LOCALE])
        assert "Traceback" not in (out + err), (out, err)
        assert code == 0, (code, out, err)
        produced = sorted(os.listdir(tmp))
        assert produced == [f"evil.{LOCALE}.svg"], produced
        assert not os.path.exists(os.path.join(os.path.dirname(tmp), f"evil.{LOCALE}.svg"))
    print(
        "  [e] `image export` with no -o writes the BASENAMED default inside the CWD "
        "(poisoned row, no traceback) OK"
    )


# --- (g) length: the gate's HIGH ---------------------------------------------
def test_length_bound_everywhere():
    """A name has to be SHORT ENOUGH TO OPEN, not just free of separators.

    Gate HIGH: `validate_document_filename` had no length rule, so a ~300-char name was
    accepted, and the composed default `<stem>.<locale>.<fmt>` then blew past the
    per-component limit -> an uncaught `OSError [Errno 22] Invalid argument` at the sink's
    `open()`. `default_out_in_cwd` checked the DIRECTORY, never that the component itself
    was openable."""
    pid = _project("length")
    doc_id = _doc(pid)

    # exactly 200 is a legitimate name and is ACCEPTED
    name_200 = "a" * 196 + ".txt"
    assert len(name_200) == 200
    with Session(engine) as s:
        content.rename_document(s, doc_id, name_200, actor="t")
    assert _filename(doc_id) == name_200

    # 201 is refused, cleanly, and does not change the row
    name_201 = "b" * 197 + ".txt"
    assert len(name_201) == 201
    with Session(engine) as s:
        raised = ""
        try:
            content.rename_document(s, doc_id, name_201, actor="t")
        except ValueError as exc:
            raised = str(exc)
    assert raised.startswith("document filename "), raised
    assert "200" in raised, raised
    assert _filename(doc_id) == name_200, "a refused rename must not change the row"

    # the INGEST seam truncates (never refuses) and KEEPS the extension
    with Session(engine) as s:
        p = s.get(Project, pid)
        d = ingest_document(s, p, "c" * 296 + ".md", MD_BYTES, TransMode.SEGMENTED)
        s.commit()
        assert len(d.filename) == 200, len(d.filename)
        assert d.filename.endswith(".md"), d.filename

    # a PRE-FIX row with a 300-char name still exports: the composer truncates the STEM,
    # never the locale or the extension, and the file lands in the CWD.
    long_doc = _doc(pid)
    _poison(long_doc, "d" * 296 + ".md")
    with _in_temp_cwd() as tmp:
        code, out, err = run_cli(["doc", "export", str(long_doc), "--locale", LOCALE])
        assert "Traceback" not in (out + err), (out, err)
        assert code == 0, (code, out, err)
        produced = os.listdir(tmp)
        assert len(produced) == 1, produced
        assert len(produced[0]) <= 200, len(produced[0])
        assert produced[0].endswith(f".{LOCALE}.md"), produced[0]
    print(
        "  [g] length rule: 200 accepted / 201 refused at rename, ingest TRUNCATES to 200 "
        "keeping the extension, a 300-char pre-fix row exports with a truncated stem OK"
    )


def test_sink_oserror_is_a_clean_error():
    """The last resort: whatever the OS says at `open()`, the operator gets one line and
    exit 2, never a traceback (gate HIGH, layer 3)."""
    import builtins

    pid = _project("oserror")
    doc_id = _doc(pid)
    real_open = builtins.open

    def _boom(path, *a, **kw):
        if str(path).endswith(f".{LOCALE}.md"):
            raise OSError(22, "Invalid argument")
        return real_open(path, *a, **kw)

    with _in_temp_cwd() as tmp:
        builtins.open = _boom
        try:
            code, out, err = run_cli(["doc", "export", str(doc_id), "--locale", LOCALE])
        finally:
            builtins.open = real_open
        assert "Traceback" not in (out + err), (out, err)
        assert code == 2, (code, out, err)
        assert "could not write" in (out + err).lower(), (out, err)
        assert os.listdir(tmp) == [], os.listdir(tmp)
    print("  [h] an OSError at the sink's open() is a clean one-line error, exit 2 OK")


def test_default_out_refused_through_a_symlink():
    """Containment is NOT redundant with sanitisation: a single-component default name
    can still resolve outside the CWD when that component IS a link. The write is refused
    (exit 2) and the external target is untouched. Skipped where the OS refuses to make
    the link (unprivileged Windows without developer mode)."""
    pid = _project("symlink")
    doc_id = _doc(pid)
    with Session(engine) as s:
        fname = s.get(Document, doc_id).filename
    composed = f"{fname.rsplit('.', 1)[0]}.{LOCALE}.md"

    outside_dir = tempfile.mkdtemp(prefix="loctms_fntrav_link_")
    target = os.path.join(outside_dir, "target.md")
    with open(target, "wb") as fh:
        fh.write(b"ORIGINAL")

    with _in_temp_cwd() as tmp:
        link = os.path.join(tmp, composed)
        made = _make_escape_link(link, target, outside_dir)
        if not made:
            print(
                "  [i] link containment SKIPPED (this OS/account refuses both a symlink "
                "and a directory junction)"
            )
            return
        # --force so the "destination exists" guard cannot shadow the containment check:
        # without default_out_in_cwd this run would follow the link and write outside.
        code, out, err = run_cli(["doc", "export", str(doc_id), "--locale", LOCALE, "--force"])
        assert "Traceback" not in (out + err), (out, err)
        assert code == 2, ("a default resolving outside the CWD must be refused", code, out, err)
        assert "outside the current directory" in (out + err), (out, err)
        _drop_link(link)  # before the CWD is reaped, so the junction is never followed
    with open(target, "rb") as fh:
        assert fh.read() == b"ORIGINAL", "the link target was overwritten"
    shutil.rmtree(outside_dir, ignore_errors=True)
    print(
        "  [i] a default output name that resolves OUTSIDE the CWD through a symlink is "
        "refused (exit 2) and the external target is untouched OK"
    )


# --- (j) the header composers share the helper (gate MED-2) ------------------
def test_download_header_names_use_the_shared_helper():
    """Not a filesystem sink (these are Content-Disposition names), but the five stem
    compositions now route through `safe_output_stem` so there is ONE rule."""
    tok = _token("fntrav-header@x.com")
    pid = _project("header")
    doc_id = _doc(pid)
    _poison(doc_id, "../../../../Windows/win.ini.md")
    r = CLIENT.get(
        f"/api/documents/{doc_id}/download",
        params={"locale": LOCALE},
        headers={"X-Loc-Token": tok},
    )
    assert r.status_code == 200, (r.status_code, r.text[:200])
    cd = r.headers.get("content-disposition", "")
    assert f'filename="win.ini.{LOCALE}.md"' in cd, cd
    assert ".." not in cd and "/" not in cd.split("filename*=")[0], cd
    print("  [j] the download header name is composed from safe_output_stem too OK")


# --- the helpers in isolation ------------------------------------------------
def test_helpers_unit():
    assert content.safe_document_filename("../../evil.txt") == "evil.txt"
    assert content.safe_document_filename("C:\\Windows\\win.ini") == "win.ini"
    assert content.safe_document_filename("\\\\srv\\share\\x.txt") == "x.txt"
    assert content.safe_document_filename("..") == "upload"
    assert content.safe_document_filename("") == "upload"
    assert content.safe_document_filename('a"b\r\n.txt') == "a_b__.txt"
    # a legitimate name is preserved verbatim
    assert content.safe_document_filename("naïve café.docx") == "naïve café.docx"
    # the sink helper takes the LAST component and drops the extension
    assert content.safe_output_stem("../../../../Windows/win.ini.txt") == "win.ini"
    assert content.safe_output_stem("..\\..\\evil.svg") == "evil"
    assert content.safe_output_stem("..") == "document"
    # the composer bounds the WHOLE component, truncating only the stem
    composed = content.safe_output_name("e" * 400 + ".md", "fr-FR", "md")
    assert len(composed) <= 200 and composed.endswith(".fr-FR.md"), composed
    assert content.safe_output_name("../../x.md", "fr-FR", "md") == "x.fr-FR.md"
    # trailing dots/spaces are dropped (Windows silently drops them, so `x.txt.` and
    # `x.txt` would be the SAME file under two different stored names)
    assert content.safe_document_filename("x.txt. ") == "x.txt"
    print("  [f] safe_document_filename / safe_output_stem unit rules OK")


def main():
    init_db()
    test_rename_refuses_traversal_names()
    test_api_and_web_rename_refuse_traversal()
    test_upload_sanitises_traversal_filename()
    test_cli_doc_export_default_out_cannot_escape_cwd()
    test_cli_image_export_default_out_cannot_escape_cwd()
    test_length_bound_everywhere()
    test_sink_oserror_is_a_clean_error()
    test_default_out_refused_through_a_symlink()
    test_download_header_names_use_the_shared_helper()
    test_helpers_unit()
    print("DOC FILENAME TRAVERSAL OK")


if __name__ == "__main__":
    main()
