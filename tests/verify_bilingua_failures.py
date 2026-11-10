"""Bilingua failures at every door.

Every BilinguaError code, a Rust panic and a missing module, injected into the engine at the
call it can come from, reach each door as a plain refusal:

  upload    web form, /api, CLI, MCP and a ZIP batch member, with `bilingua.extract` raising:
            `limit` (and a nesting cap) is the 413-class InputLimitError, every other code a
            4xx naming the file problem; nothing is stored and the Python filter is NEVER
            tried instead (no silent fallback: a refusal for safety must not be bypassed).
  export    /api, web, CLI and MCP downloads of a Bilingua document, with `extract` (the
            re-read of the stored original), `locos_set_targets` or `merge` raising: a 409
            (or 413 for a limit) with the adapter's sentence, never a 500.
  repair    reingest_document raises the LocOS refusal (or declines) and leaves the stored
            document exactly as it was, still on Bilingua.
  shadow    a shadowed upload whose Bilingua parse fails stores the Python document and logs
            only the error code.
  module    the module missing: new uploads stay Python (the one fallback); a Bilingua
            document's export is a 409 on every door.
  leaks     no door answers 500, and no answer carries a traceback or panic text; an
            `internal` error or a panic never shows the engine's message.

Runs with the real wheel when it is installed (its functions are wrapped to raise), else with
a stand-in `bilingua` module (tests/_bilingua_doors.make_fake_bilingua): the adapter, ingest,
export and all four doors are the real code either way. Hermetic + offline.
Documented in: docs/architecture/translation-pipeline.md
Final line: "BILINGUA FAILURES OK".
"""

import contextlib
import io
import logging
import os
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_bilingua_failures_")
os.environ.pop("LOC_TMS_BILINGUA_FORMATS", None)
os.environ.pop("LOC_TMS_BILINGUA_SHADOW_FORMATS", None)

from app.db import init_db
from app.filters import bilingua_adapter as ba
from tests import _bilingua_doors as D

SECRET = "Quokkasecretmarker"
MD = b"# Title\n\nHello world.\n\nSecond paragraph here.\n"
TXT = b"Plain text line one.\n"
CODES = [
    "limit",
    "parse",
    "unsupported",
    "encoding",
    "invalid_argument",
    "io",
    "error",
    "parser_version",
    "invalid_document",
    "write",
    "unwritable",
    "locked",
    "invented_tag",
    "duplicated_tag",
    "unknown_segment",
    "internal",
]
LIMIT_LIKE = {"limit", "nesting"}
QUIET = {"internal", "panic"}  # the engine's own text must never reach the user


class Engine:
    """The bilingua module the adapter will import, and a way to make one call raise."""

    def __init__(self):
        self.real = D.real_engine()
        self.mod = self.real or D.make_fake_bilingua()
        self.kind = "wheel " + self.real.version() if self.real else "stand-in module"
        self.saved: dict = {}
        if self.real is not None:
            # the adapter works on DocumentHandles (SPEED2), whose methods cannot be patched:
            # an error injected into bilingua.extract / locos_set_targets / merge reaches the
            # handle call through D.route_handles (the stand-in's handles look them up anyway)
            D.route_handles(self.real, self.saved)

    def error(self, code: str):
        if code == "panic":
            # pyo3's PanicException is matched by name (it derives from BaseException)
            return D.PanicException(f"panicked at src/lib.rs: {SECRET}")
        msg = f"{SECRET} failed here"
        if code == "nesting":
            # W2 B side: a nesting cap is code `limit` (LimitKind::Depth) in every wave-1/2
            # crate but json; the adapter branches on the code, not on words in the message
            code, msg = "limit", f"This file is nested too deeply ({SECRET})."
        base = self.mod.BilinguaError
        try:
            cls = type("InjectedBilinguaError", (base,), {})
            exc = cls(msg)
            exc.code = code
        except Exception:
            exc = base(msg)
            with contextlib.suppress(Exception):
                exc.code = code
        return exc

    def fail(self, fn: str, code: str):
        orig = self.saved.setdefault(fn, getattr(self.mod, fn))
        err = self.error(code)

        def boom(*a, **k):
            raise err

        setattr(self.mod, fn, boom)
        return orig

    def restore(self):
        for fn, orig in self.saved.items():
            setattr(self.mod, fn, orig)
        self.saved.clear()


ENG = Engine()


def switch(formats: str = "md", shadow: str = ""):
    os.environ["LOC_TMS_BILINGUA_FORMATS"] = formats
    os.environ["LOC_TMS_BILINGUA_SHADOW_FORMATS"] = shadow


def not_500(status, what):
    assert status is None or int(status) < 500, f"{what}: answered {status}"


def zip_of(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


# --------------------------------------------------------------------------- upload doors
def check_upload_refused(c, tok, srv, pid, code):
    """One code at extract, every upload door: refused, nothing stored, no Python fallback."""
    before = set(D.doc_ids(pid))
    limit = code in LIMIT_LIKE
    tag = f"upload {code}"

    st, loc, new = D.upload_web(c, pid, "w.md", MD)
    not_500(st, tag + " web")
    assert st == 303 and "err=" in loc and new is None, (tag, st, loc, new)
    assert D.no_leak(loc) and (code not in QUIET or SECRET not in loc), (tag, loc)

    st, body = D.upload_api(c, tok, pid, "a.md", MD)
    not_500(st, tag + " api")
    assert st == (413 if limit else 422), (tag, st, body)
    text = str(body)
    assert D.no_leak(text) and (code not in QUIET or SECRET not in text), (tag, body)

    rc, body, out = D.upload_cli(pid, "c.md", MD)
    assert rc not in (0, None), (tag, rc, out)
    assert D.no_leak(out) and (code not in QUIET or SECRET not in out), (tag, out)

    ok, res, statuses = D.upload_mcp(srv, pid, "m.md", MD)
    assert not ok, (tag, res)
    assert all(s < 500 for s in statuses), (tag, statuses)
    assert D.no_leak(res) and (code not in QUIET or SECRET not in res), (tag, res)

    # ZIP batch: the switched md member is refused, the unswitched txt member is a Python doc
    st, body = D.upload_api(c, tok, pid, "b.zip", zip_of([("z.md", MD), ("z.txt", TXT)]))
    not_500(st, tag + " zip")
    assert D.no_leak(str(body)) and (code not in QUIET or SECRET not in str(body)), (tag, body)
    new = sorted(set(D.doc_ids(pid)) - before)
    fmts = {D.skeleton(i).get("format") for i in new}
    stamped = [i for i in new if ba.is_bilingua_skeleton(D.skeleton(i))]
    assert not stamped, (tag, "a refused upload stored a Bilingua document")
    assert "md" not in fmts, (tag, "the md member was parsed by the Python filter: a fallback")
    if st == 200:
        assert fmts <= {"txt"}, (tag, fmts, body)
    return new


def test_upload_doors(c, tok, srv):
    pid = D.new_project("fail-upload")
    switch("md")
    for code in [*CODES, "nesting", "panic"]:
        ENG.fail("extract", code)
        try:
            new = check_upload_refused(c, tok, srv, pid, code)
        finally:
            ENG.restore()
        # only the zip's txt member may have been stored; it is a Python document
        for i in new:
            assert not ba.is_bilingua_skeleton(D.skeleton(i)), (code, i)
    # locos_units failing after a good extract is refused the same way
    ENG.fail("locos_units", "parse")
    try:
        check_upload_refused(c, tok, srv, pid, "parse")
    finally:
        ENG.restore()
    # control: with the engine healthy every door stores a Bilingua document
    st, body = D.upload_api(c, tok, pid, "ok.md", MD)
    assert st == 200 and ba.is_bilingua_skeleton(D.skeleton(body["document_id"])), (st, body)
    print(
        f"  [upload] {len(CODES) + 3} engine errors x web/api/cli/mcp/zip: refused, no fallback OK"
    )


# --------------------------------------------------------------------------- export doors
def bilingua_doc(c, tok, name="exp.md"):
    pid = D.new_project("fail-export-" + name)
    switch("md")
    st, body = D.upload_api(c, tok, pid, name, MD)
    assert st == 200, (st, body)
    doc_id = body["document_id"]
    assert ba.is_bilingua_skeleton(D.skeleton(doc_id))
    D.mock_translate(doc_id, "de-DE")
    assert any(t for *_r, t, _s in D.segments(doc_id, "de-DE")), "mock translate wrote nothing"
    return doc_id


def check_export_refused(c, tok, srv, doc_id, code, where):
    tag = f"export {code} at {where}"
    quiet = code in QUIET
    expect = 413 if code in LIMIT_LIKE else 409
    st, body = D.export_api(c, tok, doc_id, "de-DE")
    not_500(st, tag + " api")
    assert st == expect, (tag, st, body[:300])
    assert D.no_leak(body) and (not quiet or SECRET.encode() not in body), (tag, body[:300])
    if code == "unwritable":
        # the adapter's own sentence (the translation cannot go into this file), never the
        # generic "re-import" text, which would send the user the wrong way
        assert b"cannot be written" in body and b"Re-import" not in body, (tag, body[:300])
    st, body = D.export_web(c, doc_id, "de-DE")
    not_500(st, tag + " web")
    assert st == expect, (tag, st, body[:300])
    assert D.no_leak(body) and (not quiet or SECRET.encode() not in body), (tag, body[:300])
    rc, data, out = D.export_cli(doc_id, "de-DE")
    assert rc not in (0, None) and data is None, (tag, rc, out)
    assert D.no_leak(out) and (not quiet or SECRET not in out), (tag, out)
    ok, res, statuses = D.export_mcp(srv, doc_id, "de-DE")
    assert not ok, (tag, res)
    assert all(s < 500 for s in statuses), (tag, statuses)
    assert D.no_leak(res) and (not quiet or SECRET not in res), (tag, res)


def test_export_doors(c, tok, srv):
    doc_id = bilingua_doc(c, tok)
    before = D.skeleton(doc_id)
    for where in ("extract", "locos_set_targets", "merge"):
        for code in [*CODES, "panic"]:
            ENG.fail(where, code)
            try:
                check_export_refused(c, tok, srv, doc_id, code, where)
            finally:
                ENG.restore()
    assert D.skeleton(doc_id) == before, "a failed export changed the stored document"
    # control: the same document exports through every door once the engine is healthy
    st, body = D.export_api(c, tok, doc_id, "de-DE")
    assert st == 200 and body, (st, body[:200])
    rc, data, out = D.export_cli(doc_id, "de-DE")
    assert rc == 0 and data == body, (rc, out)
    print(f"  [export] {len(CODES) + 1} engine errors x 3 calls x api/web/cli/mcp: 409/413 OK")
    return doc_id


# --------------------------------------------------------------------------- repair
def test_repair(c, tok):
    from app.filters.base import InputLimitError
    from app.services.ingest import reingest_document

    pid = D.new_project("fail-repair")
    switch("md")
    st, body = D.upload_api(c, tok, pid, "r.md", MD)
    assert st == 200, body
    doc_id = body["document_id"]
    before = D.skeleton(doc_id)
    switch("")  # the switch off: the document is still Bilingua's
    for code in [*CODES, "panic"]:
        ENG.fail("extract", code)
        try:
            try:
                ok = reingest_document(doc_id)
            except (ValueError, InputLimitError) as exc:
                assert D.no_leak(str(exc)), (code, str(exc))
                assert code not in QUIET or SECRET not in str(exc), (code, str(exc))
            else:
                assert ok is False, (code, "a failed repair reported success")
        finally:
            ENG.restore()
        assert D.skeleton(doc_id) == before, (code, "a failed repair changed the document")
    assert reingest_document(doc_id) is True
    assert ba.is_bilingua_skeleton(D.skeleton(doc_id)), "a repair moved the document to Python"
    print(f"  [repair] {len(CODES) + 1} engine errors: refused, document unchanged, engine kept OK")


# --------------------------------------------------------------------------- shadow
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_shadow(c, tok):
    from app.services import bilingua_shadow

    pid = D.new_project("fail-shadow")
    switch("", shadow="md")
    cap = _Capture()
    log = logging.getLogger("app.services.bilingua_shadow")
    log.addHandler(cap)
    old_level = log.level
    log.setLevel(logging.INFO)
    every = _Capture()  # every logger: an internal error or a panic never logs engine text
    logging.getLogger().addHandler(every)
    try:
        for code in [*CODES, "panic"]:
            ENG.fail("extract", code)
            try:
                cap.lines.clear()
                every.lines.clear()
                bilingua_shadow.reset()
                st, body = D.upload_api(c, tok, pid, f"s-{code}.md", MD)
                assert st == 200, (code, st, body)
                assert bilingua_shadow.wait_idle(60), (code, "shadow never finished")
            finally:
                ENG.restore()
            sk = D.skeleton(body["document_id"])
            assert not ba.is_bilingua_skeleton(sk), (code, "shadow changed the stored document")
            recs = bilingua_shadow.recent()
            assert recs and recs[-1].get("outcome") == "refused", (code, recs)
            text = "\n".join(cap.lines)
            assert cap.lines and SECRET not in text, (code, "shadow log", text)
            assert "Hello world" not in text, (code, "the shadow log carries file text")
            if code in QUIET:
                assert SECRET not in "\n".join(every.lines), (code, "engine text logged")
    finally:
        logging.getLogger().removeHandler(every)
        log.removeHandler(cap)
        log.setLevel(old_level)
        switch("")
    print(
        f"  [shadow] {len(CODES) + 1} engine errors: upload stored on Python, log has codes only OK"
    )


# --------------------------------------------------------------------------- module missing
def test_module_missing(c, tok, srv, doc_id):
    pid = D.new_project("fail-module")
    switch("md")
    with D.engine_swap(None):
        assert not ba.available()
        st, body = D.upload_api(c, tok, pid, "x.md", MD)
        assert st == 200 and not ba.is_bilingua_skeleton(D.skeleton(body["document_id"])), body
        st, data = D.export_api(c, tok, doc_id, "de-DE")
        assert st == 409 and b"not installed" in data, (st, data[:300])
        st, data = D.export_web(c, doc_id, "de-DE")
        assert st == 409, (st, data[:300])
        rc, data, out = D.export_cli(doc_id, "de-DE")
        assert rc not in (0, None) and "not installed" in out, (rc, out)
        ok, res, statuses = D.export_mcp(srv, doc_id, "de-DE")
        assert not ok and all(s < 500 for s in statuses), (res, statuses)
    switch("")
    print("  [module] missing: new uploads stay Python, a Bilingua export is 409 on every door OK")


def main():
    init_db()
    c = D.client()
    tok = D.api_token("bili-fail@example.test")
    srv = D.mcp_server(c, tok)
    print(f"  engine: {ENG.kind}")
    with D.engine_swap(ENG.mod):
        assert ba.available(), "the engine stand-in must satisfy the adapter's API check"
        test_upload_doors(c, tok, srv)
        doc_id = test_export_doors(c, tok, srv)
        test_repair(c, tok)
        test_shadow(c, tok)
    test_module_missing(c, tok, srv, doc_id)
    os.environ.pop("LOC_TMS_BILINGUA_FORMATS", None)
    os.environ.pop("LOC_TMS_BILINGUA_SHADOW_FORMATS", None)
    print("BILINGUA FAILURES OK")


if __name__ == "__main__":
    main()
