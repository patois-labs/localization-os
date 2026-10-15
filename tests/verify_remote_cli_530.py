"""Remote CLI fixes, batch remote-db (issue #530).

Fully offline: an httpx.MockTransport fake server drives the real `--remote` CLI arm. No
network, no provider, no spend.

  1. `glossary module edit-term --remote` pages past the first (windowed) page when it
     reads the term's existing meta, so a late term keeps its metadata.
  2. `connector sync --remote` uses the route's deduplicated async mode and polls the job
     (never a synchronous POST that can outlive the client timeout); a paid synchronous
     call that does time out says the server may still be running (not "unreachable").
  3. A failed connector pull/push/sync keeps its `--json` envelope (`ok: false`).
  4. `doc export --remote` never writes to '..' / '.' / '' or a name with control chars.

Final line: "REMOTE CLI 530 OK".
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_remote530_")

import httpx

from app.cli import _remote, _remote_impl_part4
from app.cli.__main__ import main as cli_main

_remote_impl_part4._POLL_INTERVAL_S = 0.0


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _run(argv, handler):
    _remote._set_transport(httpx.MockTransport(handler))
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli_main(["--remote", "http://127.0.0.1:9", "--token", "lsu_testtoken", *argv])
    finally:
        _remote._set_transport(None)
    return rc, out.getvalue(), err.getvalue()


def _json_resp(body, status=200):
    return httpx.Response(status, json=body)


def test_edit_term_pages_past_first_window():
    seen = {"offsets": [], "edit_body": None}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/glossaries/7":
            off = int(req.url.params.get("offset", "0"))
            seen["offsets"].append(off)
            if off == 0:
                terms = [{"id": i, "meta": {}} for i in range(1, 4)]
                return _json_resp({"ok": True, "terms": terms, "truncated": True})
            late = {"definition": "keep me", "part_of_speech": "noun"}
            return _json_resp({"ok": True, "terms": [{"id": 4, "meta": late}], "truncated": False})
        if req.method == "POST" and req.url.path == "/api/glossaries/7/terms/4/edit":
            seen["edit_body"] = json.loads(req.content)
            return _json_resp({"ok": True, "entry_id": 4, "source_term": "s", "target_term": "t"})
        return _json_resp({"ok": False, "error": "unexpected " + req.url.path}, 404)

    rc, out, err = _run(["glossary", "module", "edit-term", "7", "4", "--notes", "x"], handler)
    if rc != 0:
        _fail(f"edit-term rc={rc} out={out!r} err={err!r}")
    meta = (seen["edit_body"] or {}).get("meta") or {}
    if meta.get("definition") != "keep me" or meta.get("part_of_speech") != "noun":
        _fail(f"late term lost its meta: {meta!r} (offsets read {seen['offsets']})")
    if meta.get("notes") != "x":
        _fail(f"the provided flag was not merged: {meta!r}")


def test_connector_sync_uses_async_and_polls():
    posts, gets = [], []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/api/connectors/3/sync":
            posts.append(json.loads(req.content))
            if not posts[-1].get("async"):
                # What the old CLI did: a synchronous paid run that outlives the client.
                raise httpx.ReadTimeout("slow", request=req)
            return _json_resp({"ok": True, "job_id": 41, "status_url": "/api/jobs/41"}, 202)
        if req.method == "GET" and req.url.path == "/api/jobs/41":
            gets.append(1)
            if len(gets) < 3:
                return _json_resp({"ok": True, "id": 41, "status": "running", "result": {}})
            return _json_resp(
                {
                    "ok": True,
                    "id": 41,
                    "status": "completed",
                    "result": {"ok": True, "message": "synced 5", "created": 5},
                }
            )
        return _json_resp({"ok": False, "error": "unexpected " + req.url.path}, 404)

    rc, out, err = _run(["--json", "connector", "sync", "3"], handler)
    if rc != 0:
        _fail(f"connector sync rc={rc} out={out!r} err={err!r}")
    if posts != [{"async": True}]:
        _fail(f"expected exactly one async POST, got {posts!r}")
    body = json.loads(out.strip().splitlines()[-1])
    if not body.get("ok") or body.get("summary", {}).get("created") != 5:
        _fail(f"unexpected sync envelope {body!r}")

    # A job that FAILED reports ok:false and exits 1.
    def failing(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return _json_resp({"ok": True, "job_id": 9, "existing": True}, 202)
        return _json_resp(
            {"ok": True, "id": 9, "status": "failed", "error": "bad creds", "result": {}}
        )

    rc, out, err = _run(["--json", "connector", "sync", "3"], failing)
    body = json.loads(out.strip().splitlines()[-1])
    if rc != 1 or body.get("ok") is not False or "bad creds" not in str(body.get("message")):
        _fail(f"failed job not reported: rc={rc} body={body!r}")


def test_paid_timeout_is_not_unreachable():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    rc, out, err = _run(["image", "translate", "5", "--locale", "de-DE"], handler)
    text = out + err
    if rc == 3 or "could not reach" in text:
        _fail(f"a paid timeout was reported as unreachable: rc={rc} {text!r}")
    if "still be running" not in text.replace("\n", " ").lower():
        _fail(f"a paid timeout must say the server may still be running: {text!r}")
    # A plain (cheap) read keeps the exit-3 'could not reach' contract.
    rc, out, err = _run(["glossary", "module", "show", "7"], handler)
    if rc != 3:
        _fail(f"a read timeout must still be exit 3, got {rc}: {out + err!r}")


def test_failed_pull_push_keep_json_envelope():
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_resp({"ok": False, "message": "no credentials", "pushed": 0}, 400)

    for verb in ("pull", "push", "sync"):
        if verb == "sync":

            def handler_sync(req: httpx.Request) -> httpx.Response:
                if req.method == "POST":
                    return _json_resp({"ok": True, "job_id": 2}, 202)
                return _json_resp(
                    {
                        "ok": True,
                        "status": "failed",
                        "error": "no credentials",
                        "result": {},
                    }
                )

            rc, out, err = _run(["--json", "connector", verb, "3"], handler_sync)
        else:
            rc, out, err = _run(["--json", "connector", verb, "3"], handler)
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        if rc != 1 or not lines:
            _fail(f"{verb}: rc={rc} out={out!r} err={err!r}")
        body = json.loads(lines[-1])
        if body.get("ok") is not False or body.get("message") != "no credentials":
            _fail(f"{verb}: envelope lost: {body!r}")


def _export_with_name(disposition: str, argv_extra=()):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/documents/5/download":
            return httpx.Response(
                200, content=b"DATA", headers={"content-disposition": disposition}
            )
        return _json_resp({"ok": False, "error": "unexpected"}, 404)

    return _run(["doc", "export", "5", "--locale", "fr", *argv_extra], handler)


def test_doc_export_filename_sanitised():
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            for disp in (
                'attachment; filename=".."',
                'attachment; filename="."',
                'attachment; filename="foo/"',
            ):
                rc, out, err = _export_with_name(disp, ("--force",))
                if rc != 0:
                    _fail(f"{disp}: rc={rc} out={out!r} err={err!r}")
                if not os.path.isfile("doc5.fr.bin"):
                    _fail(f"{disp}: fallback name not used, files={os.listdir('.')!r}")
                os.remove("doc5.fr.bin")
            rc, out, err = _export_with_name("attachment; filename*=UTF-8''a%1B%5B31mb.docx")
            if rc != 0:
                _fail(f"control name rc={rc} {out!r} {err!r}")
            names = os.listdir(".")
            if any(not n.isprintable() for n in names) or any("\x1b" in (out + err) for _ in "x"):
                _fail(f"control characters survived: {names!r} {out!r} {err!r}")
        finally:
            os.chdir(cwd)


def test_safe_download_name_windows_hazards():
    from app.cli._remote_impl_common_leaf import _safe_download_name as safe

    fb = "doc5.fr.bin"
    for bad in ("NUL", "nul.txt", "CON", "com1.docx", "lpt9", "...", "<>:|", ""):
        if safe(bad, fb) != fb:
            _fail(f"{bad!r} was not refused: {safe(bad, fb)!r}")
    if safe("../" + chr(92) + "..evil/report.docx", fb) != "report.docx":
        _fail("path parts survived")
    if safe("report: v2?.docx", fb) != "report v2.docx":
        _fail(f"reserved characters survived: {safe('report: v2?.docx', fb)!r}")
    if safe("console.docx", fb) != "console.docx" or safe("computer.txt", fb) != "computer.txt":
        _fail("an ordinary name that merely starts like a device name was refused")


if __name__ == "__main__":
    test_edit_term_pages_past_first_window()
    test_connector_sync_uses_async_and_polls()
    test_paid_timeout_is_not_unreachable()
    test_failed_pull_push_keep_json_envelope()
    test_doc_export_filename_sanitised()
    test_safe_download_name_windows_hazards()
    print("REMOTE CLI 530 OK")
