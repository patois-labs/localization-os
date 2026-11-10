"""Old projects keep their engine across the Bilingua switch.

  python    documents imported by the Python filters (md, json, po and wave 2 samples, UTF-8
            and legacy code pages) export byte for byte the same (untouched and mock-translated)
            after EVERY routed format is switched on; get_filter keeps the Python filter for
            them; a repair with the switch on keeps the Python skeleton and its stamps
            (read_gen, text_code_page, md_walk, ...).
  pre-1107  a skeleton stored before the 11-07 reading rules (no read_gen, no text_bom) still
            exports as before with the switch on: it never reaches the adapter.
  bilingua  a document imported with the switch on keeps Bilingua after the switch is turned
            off: same export bytes, the adapter still rebuilds it, a repair keeps the engine
            and rewrites the stamp; exports are byte-stable call after call.
  pinning   a different wheel or crate version string with the same units still exports; the
            stamped profile version is the one asked for at rebuild; units that moved, a
            changed original or a profile the wheel cannot produce refuse with re-import text
            (409 on /api, never a 500); the module missing is a 409 too.

The Bilingua half runs with the real wheel when it is installed, else with the stand-in
engine module (tests/_bilingua_doors.make_fake_bilingua), so the routing and pinning code is
proven either way; the Python half needs no wheel. Hermetic + offline.
Documented in: docs/architecture/translation-pipeline.md
Final line: "BILINGUA OLD PROJECTS OK".
"""

import copy
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_bilingua_old_projects_")
os.environ.pop("LOC_TMS_BILINGUA_FORMATS", None)
os.environ.pop("LOC_TMS_BILINGUA_SHADOW_FORMATS", None)

from sqlmodel import Session

from app.db import engine, init_db
from app.filters import bilingua_adapter as ba
from app.filters import get_filter
from app.models import Document, TransMode
from app.services import export
from app.services.export_gate import ExportUnreadableBlob
from app.services.ingest import ingest_document, reingest_document
from tests import _bilingua_doors as D

ALL_ON = ",".join(ba._FORMATS)
REPLACED: set = set()  # engine functions test_pinning replaced (D.route_handles reads it)
FILES = [
    # (name, bytes, project source locale)
    (
        "guide.md",
        b"---\ntitle: Guide\n---\n# Hello *world*\n\nSee [the docs](http://x) and `code`.\n\n"
        b"- one\n- two **bold**\n",
        "en-US",
    ),
    ("legacy.md", "# Caf\xe9 au lait\n\nNa\xefve text.\n".encode("latin-1"), "en-US"),
    ("ru.md", "# Привет мир\n\nТекст абзаца.\n".encode("cp1251"), "ru-RU"),
    ("bom.md", b"\xef\xbb\xbf# Title\r\n\r\nBody line.\r\n", "en-US"),
    (
        "strings.json",
        b'{"title": "Hello {name}", "nested": {"a": "Bye", "n": 3}, "list": ["x <b>y</b>"]}\n',
        "en-US",
    ),
    (
        "app.po",
        b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n"Language: en\\n"\n'
        b'"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n#: a.c:1\nmsgid "Hello %s"\n'
        b'msgstr ""\n\nmsgctxt "menu"\nmsgid "File"\nmsgstr ""\n\nmsgid "One file"\n'
        b'msgid_plural "%d files"\nmsgstr[0] ""\nmsgstr[1] ""\n',
        "en-US",
    ),
    (
        "ru.po",
        b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=windows-1251\\n"\n\n'
        b'msgid "' + "Привет".encode("cp1251") + b'"\nmsgstr ""\n',
        "ru-RU",
    ),
    ("notes.txt", b"First line.\nSecond line.\n", "en-US"),
    ("talk.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n", "en-US"),
    ("app.properties", b"greeting=Hello {0}\nbye=Goodbye\n", "en-US"),
]
LOCALE = "de-DE"


def switch(formats: str):
    os.environ["LOC_TMS_BILINGUA_FORMATS"] = formats


def ingest(pid, name, data):
    from app.models import Project

    with Session(engine) as s:
        doc = ingest_document(s, s.get(Project, pid), name, data, TransMode.SEGMENTED)
        s.commit()
        return doc.id


def exp(doc_id, locale=LOCALE):
    with Session(engine) as s:
        return export.build_translated_file(s, s.get(Document, doc_id), locale)


def set_skeleton(doc_id, skel):
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        d.skeleton = copy.deepcopy(skel)
        s.add(d)
        s.commit()


def filt_for(doc_id):
    return get_filter(D.skeleton(doc_id).get("format") or "", skeleton=D.skeleton(doc_id))


# --------------------------------------------------------------------------- Python half
def test_python_documents():
    switch("")
    docs = []
    for name, data, src in FILES:
        pid = D.new_project("old-" + name, targets=(LOCALE,), source=src)
        doc_id = ingest(pid, name, data)
        sk = D.skeleton(doc_id)
        assert not ba.is_bilingua_skeleton(sk), name
        untouched = exp(doc_id)
        D.mock_translate(doc_id, LOCALE)
        assert any(t for *_r, t, _s in D.segments(doc_id, LOCALE)), (name, "mock wrote nothing")
        translated = exp(doc_id)
        docs.append((name, doc_id, sk, untouched, translated))
    switch(ALL_ON)
    for name, doc_id, _sk, _untouched, translated in docs:
        f = filt_for(doc_id)
        assert f is not None and not isinstance(f, ba.BilinguaFilter), (name, type(f).__name__)
        assert exp(doc_id) == translated, (name, "a Python document's export moved")
        assert exp(doc_id) == translated, (name, "export is not byte-stable")
    # untouched: a fresh Python import of the same bytes, exported with the switch on, is the
    # bytes the switch-off export gave (the stored original is only ever read by Python)
    switch("")
    fresh = []
    for name, data, src in FILES:
        pid = D.new_project("old-untouched-" + name, targets=(LOCALE,), source=src)
        fresh.append((name, ingest(pid, name, data)))
    switch(ALL_ON)
    for (name, doc_id), (_n, _d, _sk, untouched, _t) in zip(fresh, docs, strict=True):
        assert exp(doc_id) == untouched, (name, "untouched export moved with the switch on")
    # repair with the switch on keeps the Python engine and its stamps
    for name, doc_id in fresh:
        before = D.skeleton(doc_id)
        assert reingest_document(doc_id) is True, name
        after = D.skeleton(doc_id)
        assert not ba.is_bilingua_skeleton(after), (name, "a repair moved a document to Bilingua")
        for key in ("read_gen", "text_code_page", "text_bom", "md_walk", "dialect", "format"):
            assert after.get(key) == before.get(key), (name, key, before.get(key), after.get(key))
        assert after["units"] == before["units"], name
    switch("")
    print(
        f"  [python] {len(FILES)} Python documents: exports, routing, repair unchanged "
        f"with all {len(ba._FORMATS)} formats on OK"
    )
    return docs


def test_pre_1107_skeletons(docs):
    """A skeleton stored before the 11-07 reading rules carries no read_gen / text_bom."""
    switch("")
    checked = 0
    for name, doc_id, _sk, _u, _t in docs:
        sk = D.skeleton(doc_id)
        old = {k: v for k, v in sk.items() if k not in ("read_gen", "text_bom")}
        set_skeleton(doc_id, old)
        off = exp(doc_id)
        switch(ALL_ON)
        try:
            assert not isinstance(filt_for(doc_id), ba.BilinguaFilter), name
            assert exp(doc_id) == off, (
                name,
                "a pre-11-07 skeleton exports differently with the switch on",
            )
        finally:
            switch("")
        set_skeleton(doc_id, sk)
        checked += 1
    print(
        f"  [pre-1107] {checked} skeletons without read_gen/text_bom: "
        "same export with the switch on OK"
    )


# --------------------------------------------------------------------------- Bilingua half
def bilingua_files(real: bool):
    if real:
        return [(n, d, s) for n, d, s in FILES if n.rsplit(".", 1)[-1] in ("md", "json", "po")]
    return [("fake.md", b"# Title\n\nHello world.\n\nSecond paragraph.\n", "en-US")]


def test_bilingua_documents(real: bool):
    switch("md,json,po")
    made = []
    for name, data, src in bilingua_files(real):
        pid = D.new_project("bili-" + name, targets=(LOCALE,), source=src)
        doc_id = ingest(pid, name, data)
        sk = D.skeleton(doc_id)
        assert ba.is_bilingua_skeleton(sk), (name, "the switch on did not import with Bilingua")
        for k in ("read_gen", "text_bom", "md_walk", "xliff_tokens", "docx_walk"):
            assert k not in sk, (name, k, "a Python stamp on a Bilingua document")
        assert sk["bilingua"]["original_sha256"] and sk["bilingua"]["segment_ids"], name
        untouched = exp(doc_id)
        if real:
            assert untouched == data, (name, "untouched Bilingua export is not the upload")
        D.mock_translate(doc_id, LOCALE)
        translated = exp(doc_id)
        assert translated != untouched, (name, "the mock translation is not in the export")
        made.append((name, data, doc_id, sk, translated))
    switch("")  # the kill switch: new uploads Python, Bilingua documents stay Bilingua
    for name, data, doc_id, _sk, translated in made:
        assert isinstance(filt_for(doc_id), ba.BilinguaFilter), (name, "lost its engine")
        assert exp(doc_id) == translated, (name, "export moved after the switch was turned off")
        assert exp(doc_id) == translated, (name, "export is not byte-stable")
        pid = D.new_project("bili-after-" + name, targets=(LOCALE,))
        again = ingest(pid, name, data)
        assert not ba.is_bilingua_skeleton(D.skeleton(again)), (name, "switch off still Bilingua")
    # repair never switches engine, and rewrites the stamp from the fresh parse
    name, data, _d, _sk, _t = made[0]
    pid = D.new_project("bili-repair", targets=(LOCALE,), source="en-US")
    switch("md,json,po")
    doc_id = ingest(pid, name, data)
    switch("")
    stale = D.skeleton(doc_id)
    stale["bilingua"] = {**stale["bilingua"], "wheel": "0.0.1-old"}
    set_skeleton(doc_id, stale)
    assert reingest_document(doc_id) is True
    fixed = D.skeleton(doc_id)
    assert ba.is_bilingua_skeleton(fixed), "a repair moved a Bilingua document to Python"
    assert fixed["bilingua"]["wheel"] != "0.0.1-old", "the repair kept the stale stamp"
    switch(ALL_ON)
    assert reingest_document(doc_id) is True and ba.is_bilingua_skeleton(D.skeleton(doc_id))
    switch("")
    print(
        f"  [bilingua] {len(made)} Bilingua documents keep the adapter after switch-off; "
        "repair keeps it OK"
    )
    return made


def expect_refused(doc_id, words, what):
    try:
        exp(doc_id)
    except ExportUnreadableBlob as exc:
        assert all(w in str(exc) for w in words), (what, str(exc))
    else:
        raise AssertionError(f"{what}: must be refused")


def test_pinning(made, eng):
    _name, _data, doc_id, sk, translated = made[0]
    c = D.client()
    tok = D.api_token("bili-old@example.test")
    # the wheel/crate version strings are informational: same units, same bytes
    st = copy.deepcopy(sk)
    st["bilingua"].update(wheel="0.8.9", version="0.0.1", crate=st["bilingua"].get("crate"))
    set_skeleton(doc_id, st)
    assert exp(doc_id) == translated, "a different wheel version string refused an equal document"
    # the stamped profile version is asked for at rebuild
    seen: list = []
    real_extract = eng.extract_handle  # the adapter's extraction (SPEED2 DocumentHandle)

    def spy(*a, **k):
        seen.append(k.get("profile_version"))
        return real_extract(*a, **k)

    eng.extract_handle = spy
    try:
        exp(doc_id)
    finally:
        eng.extract_handle = real_extract
    assert seen and all(v == sk["bilingua"]["profile"]["version"] for v in seen), seen
    # units that moved under this wheel: parser mismatch, re-import, 409 on /api
    real_units = eng.locos_units

    def moved(doc):
        out = real_units(doc)
        if out:
            out[0] = {**out[0], "text": out[0]["text"] + " (read differently)"}
        return out

    eng.locos_units = moved
    REPLACED.add("locos_units")
    try:
        expect_refused(doc_id, ("Re-import",), "units moved")
        r = c.get(f"/api/documents/{doc_id}/download", headers=tok, params={"locale": LOCALE})
        assert r.status_code == 409 and b"Re-import" in r.content, (r.status_code, r.content[:300])
    finally:
        eng.locos_units = real_units
        REPLACED.discard("locos_units")
    # a changed original
    st = copy.deepcopy(sk)
    st["bilingua"]["original_sha256"] = "0" * 64
    set_skeleton(doc_id, st)
    expect_refused(doc_id, ("Re-import",), "changed original")
    # a profile version this wheel cannot produce
    st = copy.deepcopy(sk)
    st["bilingua"]["profile"] = {"name": "locos", "version": 99}
    set_skeleton(doc_id, st)
    expect_refused(doc_id, ("Re-import",), "unknown profile version")
    # schema changed
    st = copy.deepcopy(sk)
    st["bilingua"]["schema"] = 999
    set_skeleton(doc_id, st)
    expect_refused(doc_id, ("Re-import",), "schema changed")
    set_skeleton(doc_id, sk)
    assert exp(doc_id) == translated
    # the module missing: 409 on /api, a clean refusal on the service
    with D.engine_swap(None):
        r = c.get(f"/api/documents/{doc_id}/download", headers=tok, params={"locale": LOCALE})
        assert r.status_code == 409 and b"not installed" in r.content, (r.status_code, r.content)
    assert exp(doc_id) == translated
    print(
        "  [pinning] version strings informational; profile version replayed; moved units, "
        "original, profile, schema, module: refused OK"
    )


def main():
    init_db()
    docs = test_python_documents()
    test_pre_1107_skeletons(docs)
    real = D.real_engine()
    eng = real or D.make_fake_bilingua()
    if real is not None:
        D.route_handles(real, REPLACED)
    print(
        "  engine for the Bilingua half: "
        + ("wheel " + real.version() if real else "stand-in module (wheel not installed)")
    )
    with D.engine_swap(eng):
        made = test_bilingua_documents(real is not None)
        test_pinning(made, eng)
    os.environ.pop("LOC_TMS_BILINGUA_FORMATS", None)
    print("BILINGUA OLD PROJECTS OK")


if __name__ == "__main__":
    main()
