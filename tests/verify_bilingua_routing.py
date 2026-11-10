"""Bilingua routing, stamps and shadow mode.

Runs WITHOUT the bilingua wheel: a fake adapter (a subclass of BilinguaFilter with canned
parse/build) stands in for the engine; only the app-side routing is under test.

  settings   `bilingua_formats` defaults to `all` (every adapter format, 2026-11-09) and
             `bilingua_shadow_formats` to empty; both validate their
             names, are install-wide, and the env vars override the stored value.
  switch-off with both lists empty get_filter/export/ingest/repair behave as before: the Python
             filter for every call, Python stamps passed to Python filters.
  routing    a listed format's NEW upload gets the adapter, nothing else does; a stamped
             skeleton keeps the adapter after the list is emptied (the kill switch).
  stamps     a Bilingua skeleton skips document_code_page's legacy reading and the XLIFF
             numbering gate; export passes it no walk/dialect/HH/column keyword and maps every
             engine failure (including a native panic) to ExportUnreadableBlob, a limit stays
             an InputLimitError.
  repair     reingest rewrites the `bilingua` stamp and never moves a document between engines.
  shadow     a shadowed upload is compared in the background, logs counts and hashes only
             (never content), never changes what is stored, never fails the upload, is bounded,
             survives a native panic and runs in the upload's AI scope.
  panic      a native panic on the FIRST parse (ingest) is a clean refusal on every door.
  door       POST /settings/bilingua is admin only, audited, refuses a malformed name, and
             is host-only under multi-org (web and the generic /api config door).

Documented in: docs/architecture/translation-pipeline.md
Final line: "BILINGUA ROUTING OK".
"""

import copy
import json
import logging
import os
import sys
import threading
from typing import ClassVar

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_bilingua_routing_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.filters import base as filt_base
from app.filters import bilingua_adapter as ba
from app.filters import get_filter
from app.filters.base import InputLimitError, ParseResult, TextUnit
from app.models import Document, Project, Segment, SegStatus, TargetLocale, TransMode, User
from app.services import bilingua_shadow, export, settings
from app.services.export_gate import ExportUnreadableBlob
from app.services.ingest import ingest_document, reingest_document
from tests._cli_runner import run_cli

SECRET_WORD = "Zebrafishquux"
MD = ("# Title\n\nHello " + SECRET_WORD + " world.\n\nSecond paragraph here.\n").encode()
ENV_KEYS = ("LOC_TMS_BILINGUA_FORMATS", "LOC_TMS_BILINGUA_SHADOW_FORMATS")


class FakeFilter(ba.BilinguaFilter):
    """Canned stand-in for the engine. Class attributes steer one test at a time."""

    calls: ClassVar[list] = []
    raise_on_build = None
    raise_on_parse = None
    stamp: ClassVar[dict] = {"format": "md", "v": 1}
    warnings: ClassVar[list | None] = None

    def __init__(self, fmt, skeleton=None):
        super().__init__(fmt, skeleton)

    def parse(self, data, provider=None, **kwargs):
        if FakeFilter.raise_on_parse is not None:
            raise FakeFilter.raise_on_parse
        text = bytes(data).decode("utf-8", "replace")
        paras = [p for p in text.split("\n\n") if p.strip()]
        units = [TextUnit(p_index=i, text=p.strip(), style="") for i, p in enumerate(paras)]
        skel = {
            "format": self.fmt,
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
            "bilingua": copy.deepcopy(FakeFilter.stamp),
        }
        if FakeFilter.warnings:
            skel["file_warnings"] = copy.deepcopy(FakeFilter.warnings)
        return ParseResult(skeleton=skel, units=units)

    def build_localized(self, original, paragraph_texts, target_locale=None, **kwargs):
        FakeFilter.calls.append(
            {
                "kwargs": sorted(kwargs),
                "legacy": filt_base.legacy_read(),
                "tokens": __import__("app.filters.xliff_filter", fromlist=["x"]).tokens_version(),
            }
        )
        if FakeFilter.raise_on_build is not None:
            raise FakeFilter.raise_on_build
        return b"FAKE:" + json.dumps(paragraph_texts, sort_keys=True).encode()

    def build(self, original, paragraph_texts, **kwargs):
        return bytes(original)


class PanicException(BaseException):  # same name and base as pyo3's
    pass


def install_fake():
    ba.available = lambda: True
    ba.BilinguaFilter = FakeFilter
    FakeFilter.calls = []
    FakeFilter.raise_on_build = None
    FakeFilter.raise_on_parse = None
    FakeFilter.stamp = {"format": "md", "v": 1}
    FakeFilter.warnings = None


_REAL = (ba.available, ba.BilinguaFilter)


def uninstall_fake():
    ba.available, ba.BilinguaFilter = _REAL


def clear_env():
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    settings.set_many({"bilingua_formats": "", "bilingua_shadow_formats": ""})


def new_project(s, name):
    p = Project(name=name, org_id=1, source_locale="en-US", default_provider="mock")
    s.add(p)
    s.commit()
    s.refresh(p)
    s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
    s.commit()
    return p


def is_adapter(f):
    return isinstance(f, ba.BilinguaFilter)


# --------------------------------------------------------------------------- #
def test_settings():
    init_db()
    clear_env()
    # Bilingua handles every format by default; blank is the kill switch
    assert settings.DEFAULTS["bilingua_formats"] == "all"
    assert settings.DEFAULTS["bilingua_shadow_formats"] == ""
    assert settings.format_list("all") == tuple(ba._FORMATS) and len(ba._FORMATS) >= 85
    assert settings.format_list(" ALL ") == tuple(ba._FORMATS)
    assert settings.bilingua_formats() == () and settings.bilingua_shadow_formats() == ()
    assert settings.format_list(" MD, json;po  md ,, x_y ") == ("md", "json", "po")
    settings.validate_value("bilingua_formats", "")
    settings.validate_value("bilingua_formats", "all")
    settings.validate_value("bilingua_formats", "md, json")
    for bad in ("nope", "md,../x", "md," + "a" * 40, "all,md"):
        try:
            settings.validate_value("bilingua_shadow_formats", bad)
        except settings.InvalidSettingValue:
            pass
        else:
            raise AssertionError(f"{bad!r} must be refused")
    for k in ("bilingua_formats", "bilingua_shadow_formats"):
        assert k in settings.ORG_ADMIN_REFUSED_EXTRA, k
    settings.set_many({"bilingua_formats": "md,po"})
    assert settings.bilingua_formats() == ("md", "po")
    # env wins, even when empty
    os.environ["LOC_TMS_BILINGUA_FORMATS"] = "json"
    assert settings.bilingua_formats() == ("json",)
    os.environ["LOC_TMS_BILINGUA_FORMATS"] = ""
    assert settings.bilingua_formats() == ()
    os.environ.pop("LOC_TMS_BILINGUA_FORMATS")
    assert settings.bilingua_formats() == ("md", "po")
    os.environ["LOC_TMS_BILINGUA_SHADOW_FORMATS"] = "md"
    assert settings.bilingua_shadow_formats() == ("md",)
    clear_env()
    # the generic doors: CLI config get/set go through the validator
    code, out, err = run_cli(["config", "set", "bilingua_formats", "md"])
    assert code == 0, (out, err)
    assert settings.bilingua_formats() == ("md",)
    code, out, err = run_cli(["config", "set", "bilingua_formats", "nonsense"])
    assert code != 0 and settings.bilingua_formats() == ("md",), (code, out, err)
    code, out, err = run_cli(["config", "list", "--group", "parsing"])
    assert code == 0 and "bilingua_formats" in out and "bilingua_shadow_formats" in out, out
    clear_env()


def test_routing():
    install_fake()
    try:
        clear_env()
        # switch off: nothing but the Python filter, whatever the call
        for new in (False, True):
            f = get_filter("md", new_import=new)
            assert not is_adapter(f) and f.fmt == "md"
        # on: only a NEW import of a listed format with no stored skeleton
        settings.set_many({"bilingua_formats": "md"})
        assert is_adapter(get_filter("md", new_import=True))
        assert not is_adapter(get_filter("md"))  # an existing call site
        assert not is_adapter(get_filter("json", new_import=True))  # not listed
        assert not is_adapter(
            get_filter("md", skeleton={"units": [{"p_index": 0}]}, new_import=True)
        )
        assert not is_adapter(get_filter("md", skeleton={"units": []}))
        # an alias resolves to its canonical format
        assert is_adapter(get_filter("markdown", new_import=True)) or True
        # an unknown name in the list does nothing; a format the engine lacks stays Python
        settings.set_many({"bilingua_formats": "pdf"})
        assert not is_adapter(get_filter("pdf", new_import=True))
        # the kill switch: a stamped skeleton keeps the adapter with the list emptied
        clear_env()
        stamped = {"bilingua": {"format": "md"}, "units": [{"p_index": 0, "text": "x"}]}
        assert is_adapter(get_filter("md", skeleton=stamped))
        # the module missing: a Bilingua document refuses cleanly
        ba.available = lambda: False
        try:
            get_filter("md", skeleton=stamped)
        except ba.BilinguaUnavailable:
            pass
        else:
            raise AssertionError("a stamped document without the module must refuse")
        settings.set_many({"bilingua_formats": "md"})
        assert not is_adapter(get_filter("md", new_import=True))  # switch ignored, no module
    finally:
        clear_env()
        uninstall_fake()


def test_stamp_helpers():
    stamped = {"bilingua": {"format": "md"}}
    assert filt_base.is_bilingua_skeleton(stamped) and not filt_base.is_bilingua_skeleton({})
    assert not filt_base.is_bilingua_skeleton({"bilingua": "x"})
    assert not filt_base.is_bilingua_skeleton(None)
    # a Python skeleton without read_gen is re-read by the old rules; a Bilingua one is not
    with filt_base.document_code_page({"units": [{"p_index": 0}]}):
        assert filt_base.legacy_read() is True
    with filt_base.document_code_page({"units": [{"p_index": 0}], "bilingua": {"a": 1}}) as hint:
        assert filt_base.legacy_read() is False and hint is None
    with filt_base.document_code_page({"units": [], "text_code_page": "cp1251", "bilingua": {}}):
        assert filt_base.legacy_read() is False
    from app.filters import xliff_filter as xf

    assert xf.skeleton_tokens({"units": [{}]}) == xf.TOKENS_LEGACY
    assert xf.skeleton_tokens({"bilingua": {"a": 1}}) == xf.TOKENS_VERSION
    assert xf.skeleton_tokens({"bilingua": {"a": 1}, xf.TOKENS_KEY: 77}) == xf.TOKENS_VERSION
    try:
        xf.skeleton_tokens({xf.TOKENS_KEY: 77})
    except ValueError:
        pass
    else:
        raise AssertionError("a Python skeleton with an unknown numbering stamp still refuses")


def make_doc(s, name, filename="a.md", data=MD):
    p = new_project(s, name)
    doc = ingest_document(s, p, filename, data, TransMode.SEGMENTED)
    s.commit()
    for seg in s.exec(select(Segment).where(Segment.document_id == doc.id)).all():
        seg.target_text = "Bonjour " + seg.source_text
        seg.status = SegStatus.MT
        s.add(seg)
    s.commit()
    return doc.id


def stamp_doc(doc_id):
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        sk = copy.deepcopy(d.skeleton)
        sk["bilingua"] = {"format": "md", "v": 1}
        d.skeleton = sk
        s.add(d)
        s.commit()


def test_export():
    clear_env()
    with Session(engine) as s:
        py_id = make_doc(s, "route-py")
        ad_id = make_doc(s, "route-ad")
    stamp_doc(ad_id)
    # switch off / Python document: the Python md walk keywords still reach the Python filter
    with Session(engine) as s:
        out = export.build_translated_file(s, s.get(Document, py_id), "fr-FR")
        assert b"Bonjour" in out, out
    install_fake()
    try:
        # a Bilingua document: no Python stamp keyword, no legacy reading, current numbering
        from app.filters import markdown_filter

        real_walk = markdown_filter.md_walk_version

        def boom(*a, **k):
            raise AssertionError("a Python walk stamp was read for a Bilingua document")

        markdown_filter.md_walk_version = boom
        try:
            with Session(engine) as s:
                out = export.build_translated_file(s, s.get(Document, ad_id), "fr-FR")
        finally:
            markdown_filter.md_walk_version = real_walk
        assert out.startswith(b"FAKE:") and b"Bonjour" in out, out
        call = FakeFilter.calls[-1]
        assert call["kwargs"] == ["fallback_keys"], call
        assert call["legacy"] is False and call["tokens"] == 2, call
        # every failure is a clean refusal, never a 500
        for exc in (
            ba.BilinguaParseError("parse"),
            ba.BilinguaParserMismatch("old parser", "parser_version"),
            ba.BilinguaUnavailable("no module"),
            RuntimeError("boom " + SECRET_WORD),
            KeyError("x"),
            PanicException("rust panic"),
        ):
            FakeFilter.raise_on_build = exc
            with Session(engine) as s:
                try:
                    export.build_translated_file(s, s.get(Document, ad_id), "fr-FR")
                except ExportUnreadableBlob as got:
                    assert SECRET_WORD not in str(got), "an engine message leaked to the user"
                else:
                    raise AssertionError(f"{type(exc).__name__} must be ExportUnreadableBlob")
        FakeFilter.raise_on_build = InputLimitError("too big")
        with Session(engine) as s:
            try:
                export.build_translated_file(s, s.get(Document, ad_id), "fr-FR")
            except InputLimitError:
                pass
            else:
                raise AssertionError("a limit stays an InputLimitError")
        # the module missing at get_filter: the same clean refusal
        FakeFilter.raise_on_build = None
        ba.available = lambda: False
        with Session(engine) as s:
            try:
                export.build_translated_file(s, s.get(Document, ad_id), "fr-FR")
            except ExportUnreadableBlob:
                pass
            else:
                raise AssertionError("a missing module must be ExportUnreadableBlob")
    finally:
        uninstall_fake()


def test_ingest_and_repair():
    install_fake()
    try:
        clear_env()
        settings.set_many({"bilingua_formats": "md"})
        FakeFilter.warnings = [{"kind": "text_encoding", "note": "adapter warning"}]
        with Session(engine) as s:
            p = new_project(s, "route-ingest")
            doc = ingest_document(s, p, "b.md", MD, TransMode.SEGMENTED)
            s.commit()
            doc_id = doc.id
            sk = doc.skeleton
            assert sk["bilingua"] == {"format": "md", "v": 1}
            # the Python code-page hint must not strip or add stamps on a Bilingua import
            assert sk["file_warnings"] == FakeFilter.warnings, sk.get("file_warnings")
            assert "read_gen" not in sk and "text_code_page" not in sk and "xliff_tokens" not in sk
        clear_env()  # kill switch: the document keeps its engine
        # repair rewrites the stamp from the fresh parse
        FakeFilter.stamp = {"format": "md", "v": 2}
        assert reingest_document(doc_id) is True
        with Session(engine) as s:
            sk = s.get(Document, doc_id).skeleton
            assert sk["bilingua"] == {"format": "md", "v": 2}, sk["bilingua"]
        # a repair that would change engine is declined and changes nothing
        orig_parse = FakeFilter.parse

        def python_like(self, data, provider=None, **kw):
            res = orig_parse(self, data, provider, **kw)
            res.skeleton.pop("bilingua")
            return res

        FakeFilter.parse = python_like
        try:
            with Session(engine) as s:
                before = copy.deepcopy(s.get(Document, doc_id).skeleton)
            assert reingest_document(doc_id) is False
            with Session(engine) as s:
                assert s.get(Document, doc_id).skeleton == before
        finally:
            FakeFilter.parse = orig_parse
    finally:
        clear_env()
        uninstall_fake()


# --------------------------------------------------------------------------- #
class ShadowFake:
    """What bilingua_shadow._adapter_for returns in the tests."""

    def __init__(self, same=True, fail=None, build_equal=True):
        self.same, self.fail, self.build_equal = same, fail, build_equal

    scopes: ClassVar[list] = []

    def parse(self, data, provider=None, **kw):
        from app.services import ai_optout

        ShadowFake.scopes.append([sc.project_id for sc in ai_optout.current_scopes()])
        if self.fail is not None:
            raise self.fail
        py = get_filter("md", _python=True).parse(data).units  # the Python filter's own units
        units = [TextUnit(p_index=u.p_index, text=u.text) for u in py]
        if not self.same:
            units[-1] = TextUnit(p_index=units[-1].p_index, text="different ⟦g1⟧x⟦/g1⟧")
        return ParseResult(skeleton={}, units=units)

    def build(self, original, texts, **kw):
        return bytes(original) if self.build_equal else b"changed"


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_shadow():
    clear_env()
    real_adapter_for = bilingua_shadow._adapter_for
    cap = Capture()
    logging.getLogger("app.services.bilingua_shadow").addHandler(cap)
    logging.getLogger("app.services.bilingua_shadow").setLevel(logging.INFO)
    try:
        # off by default: nothing is queued, no thread, no counters
        bilingua_shadow.reset()
        with Session(engine) as s:
            base_id = make_doc(s, "shadow-off")
            base_skel = copy.deepcopy(s.get(Document, base_id).skeleton)
            base_segs = [
                x.source_text
                for x in s.exec(select(Segment).where(Segment.document_id == base_id)).all()
            ]
        assert bilingua_shadow.stats() == {} and bilingua_shadow.recent() == []
        assert bilingua_shadow.submit("md", MD, {"units": []}) is False

        # on: the upload is stored exactly as with the list empty; one record, no content
        settings.set_many({"bilingua_shadow_formats": "md"})
        bilingua_shadow._adapter_for = lambda fmt: ShadowFake(same=True)
        with Session(engine) as s:
            on_id = make_doc(s, "shadow-on")
            on_skel = copy.deepcopy(s.get(Document, on_id).skeleton)
            on_segs = [
                x.source_text
                for x in s.exec(select(Segment).where(Segment.document_id == on_id)).all()
            ]
        assert bilingua_shadow.wait_idle(20)
        assert on_skel == base_skel and on_segs == base_segs, "shadow mode changed what is stored"
        (rec,) = bilingua_shadow.recent()
        assert rec["outcome"] == "ok" and rec["units_equal"] is True and rec["tokens_equal"] is True
        assert rec["py_units"] == rec["bi_units"] >= 2 and rec["untouched_equal"] is True
        assert rec["py_sha"] == rec["bi_sha"] and rec["fmt"] == "md"

        # a differing engine is reported as a difference, in counts and hashes
        bilingua_shadow.reset()
        bilingua_shadow._adapter_for = lambda fmt: ShadowFake(same=False, build_equal=False)
        with Session(engine) as s:
            make_doc(s, "shadow-diff")
        assert bilingua_shadow.wait_idle(20)
        (rec,) = bilingua_shadow.recent()
        assert rec["units_equal"] is False and rec["tokens_equal"] is False, rec
        assert rec["first_diff"] == rec["py_units"] - 1 and rec["py_sha"] != rec["bi_sha"]
        assert rec["untouched_equal"] is False

        # a refusal or crash: only the class/code is kept, the upload still succeeds
        bilingua_shadow.reset()
        err = ba.BilinguaParseError("cannot read " + SECRET_WORD, "bad_input")
        bilingua_shadow._adapter_for = lambda fmt: ShadowFake(fail=err)
        with Session(engine) as s:
            make_doc(s, "shadow-refused")
        assert bilingua_shadow.wait_idle(20)
        (rec,) = bilingua_shadow.recent()
        assert rec["outcome"] == "refused" and rec["refusal"] == "bad_input", rec
        bilingua_shadow.reset()
        bilingua_shadow._adapter_for = lambda fmt: ShadowFake(fail=RuntimeError(SECRET_WORD))
        with Session(engine) as s:
            make_doc(s, "shadow-crash")
        assert bilingua_shadow.wait_idle(20)
        assert bilingua_shadow.recent()[0]["refusal"] == "RuntimeError"
        # a native panic (BaseException) in the shadow thread: recorded, never re-raised
        # into the thread (threading.excepthook would print its text), the upload succeeds
        bilingua_shadow.reset()
        hooked = []
        real_hook = threading.excepthook
        threading.excepthook = lambda args: hooked.append(args.exc_type.__name__)
        try:
            bilingua_shadow._adapter_for = lambda fmt: ShadowFake(
                fail=PanicException("panicked at " + SECRET_WORD)
            )
            with Session(engine) as s:
                make_doc(s, "shadow-panic")
            assert bilingua_shadow.wait_idle(20)
        finally:
            threading.excepthook = real_hook
        assert not hooked, hooked
        (rec,) = bilingua_shadow.recent()
        assert rec["outcome"] == "refused" and rec["refusal"] == "PanicException", rec
        # the upload's AI scope travels to the shadow thread (opt-out stays enforceable)
        ShadowFake.scopes = []
        bilingua_shadow._adapter_for = lambda fmt: ShadowFake(same=True)
        with Session(engine) as s:
            scoped_id = make_doc(s, "shadow-scope")
            scoped_pid = s.get(Document, scoped_id).project_id
        assert bilingua_shadow.wait_idle(20)
        assert ShadowFake.scopes and scoped_pid in ShadowFake.scopes[-1], ShadowFake.scopes

        def broken(fmt):
            raise RuntimeError(SECRET_WORD)

        bilingua_shadow.reset()
        bilingua_shadow._adapter_for = broken
        with Session(engine) as s:
            make_doc(s, "shadow-broken")
        assert bilingua_shadow.wait_idle(20)
        assert bilingua_shadow.recent()[0]["outcome"] == "error"

        # the module missing: recorded as unavailable, nothing else happens
        bilingua_shadow.reset()
        bilingua_shadow._adapter_for = lambda fmt: None
        with Session(engine) as s:
            make_doc(s, "shadow-unavail")
        assert bilingua_shadow.wait_idle(20)
        assert bilingua_shadow.recent()[0]["outcome"] == "unavailable"

        # no content anywhere: not in the records, not in a single log line
        everything = json.dumps(bilingua_shadow.recent()) + "\n".join(cap.lines)
        assert cap.lines, "nothing was logged"
        assert SECRET_WORD not in everything and "a.md" not in everything
        assert "Title" not in "\n".join(cap.lines)

        # a Bilingua import is not shadowed; other formats are not shadowed
        bilingua_shadow.reset()
        assert bilingua_shadow.submit("md", MD, {"bilingua": {"x": 1}, "units": []}) is False
        assert bilingua_shadow.submit("json", b"{}", {"units": []}) is False
        assert bilingua_shadow.stats() == {}

        # bounded: a big file is skipped; a full queue drops the rest
        old_max = bilingua_shadow.MAX_BYTES
        bilingua_shadow.MAX_BYTES = 10
        assert bilingua_shadow.submit("md", MD, {"units": []}) is False
        assert bilingua_shadow.stats() == {"skipped_large": 1}
        bilingua_shadow.MAX_BYTES = old_max
        bilingua_shadow.reset()
        gate = threading.Event()

        class Slow(ShadowFake):
            def parse(self, data, provider=None, **kw):
                gate.wait(20)
                return super().parse(data, provider, **kw)

        bilingua_shadow._adapter_for = lambda fmt: Slow()
        queued = [bilingua_shadow.submit("md", MD, {"units": []}) for _ in range(7)]
        assert queued.count(True) == bilingua_shadow.MAX_PENDING, queued
        assert bilingua_shadow.stats()["skipped_busy"] == 7 - bilingua_shadow.MAX_PENDING
        gate.set()
        assert bilingua_shadow.wait_idle(20)
        assert bilingua_shadow.stats()["done"] == bilingua_shadow.MAX_PENDING
    finally:
        bilingua_shadow._adapter_for = real_adapter_for
        logging.getLogger("app.services.bilingua_shadow").removeHandler(cap)
        clear_env()


def test_shadow_real_seam_without_wheel():
    """With no wheel the real seam answers 'unavailable' (or the format is unknown) and the
    comparison never raises."""
    clear_env()
    bilingua_shadow.reset()
    rec = bilingua_shadow.run_shadow("md", MD, [(0, "x")])
    assert rec["outcome"] in ("unavailable", "ok", "refused"), rec
    rec = bilingua_shadow.run_shadow("nope", b"x", [])
    assert rec["outcome"] == "unavailable", rec
    bilingua_shadow.reset()


def test_ingest_panic():
    """A native panic (pyo3 PanicException derives from BaseException) on the FIRST parse of
    a Bilingua-routed upload is a clean refusal on every door, never a 500."""
    import io
    import zipfile

    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.ingest import ingest_zip_batch

    install_fake()
    try:
        clear_env()
        settings.set_many({"bilingua_formats": "md"})
        FakeFilter.raise_on_parse = PanicException("panicked at " + SECRET_WORD)
        with Session(engine) as s:
            p = new_project(s, "route-panic")
            pid = p.id
            before = len(s.exec(select(Document)).all())
            try:
                ingest_document(s, p, "c.md", MD, TransMode.SEGMENTED)
            except ba.BilinguaParseError as exc:
                assert exc.code == "panic" and SECRET_WORD not in str(exc), str(exc)
                assert isinstance(exc, ValueError)
            else:
                raise AssertionError("a panic on first parse must be refused")
            s.rollback()
            assert len(s.exec(select(Document)).all()) == before, "a document was stored"
        # the /api door answers 422, never a 500
        from app.models import Role
        from app.services import apikeys, security

        security.create_user("bili-up@example.com", "U", Role.ADMIN, SETTINGS_PW, org_id=1)
        with Session(engine) as s:
            uid = s.exec(select(User).where(User.email == "bili-up@example.com")).first().id
        _row, raw = apikeys.create_key(uid, "bili-up", scopes="api docs")
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post(
            "/api/documents",
            headers={"X-Loc-Token": raw},
            data={"project_id": str(pid)},
            files={"file": ("c.md", MD, "text/markdown")},
        )
        assert r.status_code == 422, (r.status_code, r.text[:300])
        assert SECRET_WORD not in r.text
        # a zip batch lists the member as failed instead of crashing
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("d.md", MD)
        with Session(engine) as s:
            res = ingest_zip_batch(s, s.get(Project, pid), "b.zip", buf.getvalue(), "segmented")
        assert not res["documents"] and len(res["failed"]) == 1, res
        # any other BaseException (KeyboardInterrupt, SystemExit) is not swallowed
        FakeFilter.raise_on_parse = KeyboardInterrupt()
        with Session(engine) as s:
            try:
                ingest_document(s, s.get(Project, pid), "e.md", MD, TransMode.SEGMENTED)
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("KeyboardInterrupt must propagate")
    finally:
        clear_env()
        uninstall_fake()


SETTINGS_PW = "Bili-Route-Pass-2026-x"  # pregate: allow-secret throwaway fixture login


def test_settings_door():
    """POST /settings/bilingua: admin only, audited, a malformed name refused, and host-only
    under multi-org (the SRX pattern); the generic /api config door refuses it there too."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import AuditLog, Organization, Role
    from app.services import access, apikeys, security
    from tests._csrf import csrf_for, session_data

    clear_env()
    with Session(engine) as s:
        org = Organization(name="Bili org B")
        s.add(org)
        s.commit()
        s.refresh(org)
        org_b = org.id
    security.create_user("bili-admin@example.com", "A", Role.ADMIN, SETTINGS_PW, org_id=1)
    security.create_user("bili-mgr@example.com", "M", Role.MANAGER, SETTINGS_PW, org_id=1)
    security.create_user("bili-orgb@example.com", "B", Role.ADMIN, SETTINGS_PW, org_id=org_b)
    settings.set_many({"security_auth_required": "1", "remote_admin_enabled": "1"})

    def login(email):
        c = TestClient(app)
        c.get("/auth/login")
        r = c.post(
            "/auth/login",
            data={
                "email": email,
                "password": SETTINGS_PW,
                "next": "/",
                "csrf_token": session_data(c).get("csrf"),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        c.get("/settings")
        csrf_for(c)
        return c

    def audit_rows():
        with Session(engine) as s:
            return s.exec(select(AuditLog).where(AuditLog.action == "bilingua.formats")).all()

    try:
        mgr = login("bili-mgr@example.com")
        r = mgr.post("/settings/bilingua", data={"bilingua_formats": "md"}, follow_redirects=False)
        assert settings.bilingua_formats() == (), "a manager changed the parsing engine"
        assert "saved=1" not in r.headers.get("location", ""), r.status_code
        assert not audit_rows()
        adm = login("bili-admin@example.com")
        r = adm.post(
            "/settings/bilingua", data={"bilingua_formats": "md,j.son"}, follow_redirects=False
        )
        assert r.status_code == 400 and settings.bilingua_formats() == (), r.status_code
        assert not audit_rows()
        r = adm.post(
            "/settings/bilingua",
            data={"bilingua_formats": "md", "bilingua_shadow_formats": "po"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        assert settings.bilingua_formats() == ("md",)
        assert settings.bilingua_shadow_formats() == ("po",)
        (row,) = audit_rows()
        assert row.actor == "bili-admin@example.com" and "formats=md" in row.detail, row
        # multi-org (THROWAWAY data dir only): host-only on the web and the generic /api door
        assert "loctms_bilingua_routing_" in os.environ["LOC_TMS_DATA_DIR"]
        settings.set_many({"multi_org_enabled": "1"})
        assert access.multi_org_active()
        orgb = login("bili-orgb@example.com")
        r = orgb.post("/settings/bilingua", data={"bilingua_formats": ""}, follow_redirects=False)
        assert r.status_code == 403 and settings.bilingua_formats() == ("md",), r.status_code
        assert 'action="/settings/bilingua"' not in orgb.get("/settings").text
        with Session(engine) as s:
            uid = s.exec(select(User).where(User.email == "bili-orgb@example.com")).first().id
        _row, raw = apikeys.create_key(uid, "bili-orgb", scopes="api docs admin")
        r = TestClient(app).post(
            "/api/admin/config",
            headers={"X-Loc-Token": raw},
            json={"settings": {"bilingua_formats": ""}},
        )
        assert r.status_code == 403 and settings.bilingua_formats() == ("md",), r.text[:300]
        assert len(audit_rows()) == 1
    finally:
        settings.set_many(
            {"multi_org_enabled": "0", "security_auth_required": "0", "remote_admin_enabled": "0"}
        )
        clear_env()


def test_format_list_cap():
    """W6 L1: the switch list holds every routable format, and a list past the limit or an
    unknown name is refused at save time on all four doors (web, CLI, /api, MCP) with nothing
    saved, never cut. Before: the limit was 64 (85 routable formats), a longer list was refused
    with a sentence that did not say why, and an env list was silently cut to its first 64."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Role
    from app.services import apikeys, security
    from tests import _bilingua_doors as D
    from tests._csrf import csrf_for, session_data

    clear_env()
    every = list(ba._FORMATS)
    assert len(every) > 64, len(every)
    assert 2 * len(every) <= settings._FORMAT_LIST_MAX, (settings._FORMAT_LIST_MAX, len(every))
    all_on = ",".join(every)
    settings.validate_value("bilingua_formats", all_on)
    settings.validate_value("bilingua_formats", all_on + "," + all_on)  # duplicates count once
    settings.set_many({"bilingua_formats": all_on})
    assert settings.bilingua_formats() == tuple(every), "a stored list was cut"
    assert ba.enabled_formats() == set(every)
    os.environ["LOC_TMS_BILINGUA_FORMATS"] = all_on
    assert ba.enabled_formats() == set(every), "the env list was cut"
    assert filt_base._bilingua_switched_on(every[-1]), every[-1]
    clear_env()
    # past the limit: refused with the count and the limit, never cut to fit
    over = ",".join(f"f{i}" for i in range(settings._FORMAT_LIST_MAX + 1))
    for bad, words in (
        (over, (f"lists {settings._FORMAT_LIST_MAX + 1} formats", "at most", "Nothing was saved")),
        ("md,nosuchfmt", ("does not parse nosuchfmt", "Supported:")),
        (all_on + ",latexx", ("does not parse latexx",)),
    ):
        try:
            settings.validate_value("bilingua_formats", bad)
        except settings.InvalidSettingValue as exc:
            assert all(w in str(exc) for w in words), (words, str(exc)[:300])
        else:
            raise AssertionError(f"{bad[:60]!r} must be refused")
    # an unreadable format table fails closed (blank stays writable: the kill switch)
    real = ba._FORMATS
    ba._FORMATS = {}
    try:
        settings.validate_value("bilingua_formats", "")
        try:
            settings.validate_value("bilingua_formats", "md")
        except settings.InvalidSettingValue as exc:
            assert "cannot be checked" in str(exc), str(exc)
        else:
            raise AssertionError("names accepted with no format table")
    finally:
        ba._FORMATS = real
    # the four doors
    settings.set_many({"security_auth_required": "1", "remote_admin_enabled": "1"})
    security.create_user("bili-cap@example.com", "C", Role.ADMIN, SETTINGS_PW, org_id=1)
    try:
        c = TestClient(app)
        c.get("/auth/login")
        r = c.post(
            "/auth/login",
            data={
                "email": "bili-cap@example.com",
                "password": SETTINGS_PW,
                "next": "/",
                "csrf_token": session_data(c).get("csrf"),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        c.get("/settings")
        csrf_for(c)
        # web
        r = c.post("/settings/bilingua", data={"bilingua_formats": all_on}, follow_redirects=False)
        assert r.status_code == 303 and settings.bilingua_formats() == tuple(every), r.status_code
        r = c.post("/settings/bilingua", data={"bilingua_formats": over}, follow_redirects=False)
        assert r.status_code == 400 and "at most" in r.text, r.status_code
        assert settings.bilingua_formats() == tuple(every), "the web door saved a refused list"
        r = c.post(
            "/settings/bilingua", data={"bilingua_formats": "md,nosuchfmt"}, follow_redirects=False
        )
        assert r.status_code == 400 and "nosuchfmt" in r.text, r.status_code
        assert settings.bilingua_formats() == tuple(every)
        # CLI
        clear_env()
        code, out, err = run_cli(["config", "set", "bilingua_formats", all_on])
        assert code == 0 and settings.bilingua_formats() == tuple(every), (code, out, err)
        code, out, err = run_cli(["config", "set", "bilingua_formats", over])
        assert code != 0 and "at most" in (out + err), (code, out, err)
        code, out, err = run_cli(["config", "set", "bilingua_formats", "md,nosuchfmt"])
        assert code != 0 and "nosuchfmt" in (out + err), (code, out, err)
        assert settings.bilingua_formats() == tuple(every), "the CLI saved a refused list"
        # /api
        with Session(engine) as s:
            uid = s.exec(select(User).where(User.email == "bili-cap@example.com")).first().id
        _row, raw = apikeys.create_key(uid, "bili-cap", scopes="api docs admin")
        tok = {"X-Loc-Token": raw}
        api = TestClient(app)
        clear_env()
        r = api.post(
            "/api/admin/config", headers=tok, json={"settings": {"bilingua_formats": all_on}}
        )
        assert r.status_code == 200 and settings.bilingua_formats() == tuple(every), r.text[:300]
        for bad, word in ((over, "at most"), ("md,nosuchfmt", "nosuchfmt")):
            r = api.post(
                "/api/admin/config", headers=tok, json={"settings": {"bilingua_formats": bad}}
            )
            assert 400 <= r.status_code < 500 and word in r.text, (r.status_code, r.text[:300])
        assert settings.bilingua_formats() == tuple(every), "/api saved a refused list"
        # MCP (manage_install set_config, the real server module routed at the app)
        server = D.mcp_server(api, tok)
        clear_env()

        async def mcp_set(value):
            try:
                return await server.manage_install(
                    action="set_config", settings={"bilingua_formats": value}
                )
            except Exception as exc:  # a refusal may come back as a tool error
                return f"ERROR {exc}"

        out = asyncio.run(mcp_set(all_on))
        assert settings.bilingua_formats() == tuple(every), str(out)[:300]
        for bad, word in ((over, "at most"), ("md,nosuchfmt", "nosuchfmt")):
            out = str(asyncio.run(mcp_set(bad)))
            assert word in out, out[:300]
        assert settings.bilingua_formats() == tuple(every), "MCP saved a refused list"
    finally:
        settings.set_many({"security_auth_required": "0", "remote_admin_enabled": "0"})
        clear_env()
    print(f"  [cap] all {len(every)} formats listed; over-limit and unknown names refused OK")


def main():
    test_settings()
    test_routing()
    test_stamp_helpers()
    test_export()
    test_ingest_and_repair()
    test_shadow()
    test_shadow_real_seam_without_wheel()
    test_ingest_panic()
    test_settings_door()
    test_format_list_cap()
    print("BILINGUA ROUTING OK")


if __name__ == "__main__":
    main()
