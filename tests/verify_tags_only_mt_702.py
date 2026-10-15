"""A segment with nothing but inline tags is copied, never sent to the MT engine (#702).

The format soak's real Trados file `sdlxliff/3` is one 15,112-character unit made of 1,159
`⟦xN⟧` placeholder tags separated by tabs and new lines, with no text at all. The plain MT job
sent it to the local engine in three #683 pieces: 20 minutes, ~40,000 tokens, and the
answer was the source again. Meanwhile the job showed 0/0 progress (the total was only
reported after the first segment finished), so it looked stuck.

  job        an SDLXLIFF file in the sdlxliff/3 shape (no seg-source) with two tags-only units
             (`<x/>` codes and whitespace, one long enough to be chunked) and a text unit: the
             counting fake engine never sees a tags-only text, the tags-only targets equal
             their sources exactly (status MT, nothing held), the text unit is translated,
             the summary counts `tags_only`, and the export keeps the `<x/>` codes
  progress   the job reports `0/total` before the first segment finishes
  helper     `mt_chunking.tags_only`: tags + whitespace only -> True; any letter, digit or
             punctuation outside the tags, no tag at all, or an empty text -> False

Every engine is a local fake (no network, no spend). Final line: "TAGS ONLY MT 702 OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tagsonly702_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import Document, JobStatus, Segment, SegStatus
from app.services import export, jobs, mt_chunking, providers_hub
from app.services.providers import Provider, register_provider

init_db()
_client = TestClient(app)
_N = [0]
_TOKEN_RE = re.compile(r"«TAG[0-9a-f]+_\d+»|⟦/?[a-z]?\d+⟧")


def _fail(msg):
    print("FAIL:", str(msg).encode("ascii", "backslashreplace").decode("ascii"))
    sys.exit(1)


class _CountingEngine(Provider):
    """Records every text it is asked to translate; returns 'FR ' + the text."""

    name = "countfake"

    def __init__(self):
        self.calls: list[str] = []

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        text = (payload or {}).get("text", "")
        self.calls.append(text)
        return "Bonjour " + text


def _install(eng):
    names = sorted({"mock", (config.DEFAULT_PROVIDER or "mock").lower()})
    saved = {n: providers_hub._OVERRIDES.get(n) for n in names}
    for n in names:
        register_provider(n, eng)
    return saved


def _restore(saved):
    for n, prev in saved.items():
        if prev is None:
            providers_hub._OVERRIDES.pop(n, None)
        else:
            providers_hub._OVERRIDES[n] = prev


def _upload(name, data: bytes) -> int:
    _N[0] += 1
    r = _client.post(
        "/projects",
        data={
            "name": f"tagsonly-{_N[0]}",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    r = _client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": (name, data, "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    return int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])


def _xliff() -> bytes:
    short = '<x id="1"/>\n\t\t<x id="2"/>'
    # Long enough that #683 would chunk it (an estimate over the 4,096-token budget).
    long_ = "\n\t\t".join(f'<x id="{i}"/>' for i in range(3, 1700))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xliff sdl:version="1.0" version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2" '
        'xmlns:sdl="http://sdl.com/FileTypes/SdlXliff/1.0">\n'
        '<file original="t" source-language="en-US" target-language="fr-FR" '
        'datatype="x-sdlfilterframework2">'
        "<body>\n"
        f'<trans-unit id="a"><source>{short}</source></trans-unit>\n'
        '<trans-unit id="b"><source>Save the <x id="9000"/> file.</source></trans-unit>\n'
        f'<trans-unit id="c"><source>{long_}</source></trans-unit>\n'
        "</body></file></xliff>\n"
    ).encode()


def _segs(doc_id):
    with Session(engine) as s:
        return s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == "fr-FR")
            .order_by(Segment.order_index)
        ).all()


def test_job():
    doc_id = _upload("tags.sdlxliff", _xliff())
    segs = _segs(doc_id)
    tag_segs = [g for g in segs if mt_chunking.tags_only(g.source_text)]
    text_segs = [g for g in segs if not mt_chunking.tags_only(g.source_text)]
    if len(tag_segs) != 2 or len(text_segs) != 1:
        _fail(f"expected 2 tags-only units and 1 text unit: {[g.source_text[:40] for g in segs]}")
    if max(len(g.source_text) for g in tag_segs) < 15000:
        _fail("the long tags-only unit must be long enough to be chunked")
    eng = _CountingEngine()
    saved = _install(eng)
    try:
        job = jobs.run_sync("translate_document", {"document_id": doc_id, "locale": "fr-FR"})
    finally:
        _restore(saved)
    if job.status != JobStatus.COMPLETED:
        _fail(f"[job] the translate job must complete, got {job.status}: {job.error}")
    for c in eng.calls:
        if not _TOKEN_RE.sub("", c).strip():
            _fail(f"[job] the engine was sent a tags-only text ({len(c)} chars): {c[:60]!r}")
    if len(eng.calls) != 1 or "Save the" not in eng.calls[0]:
        _fail(f"[job] exactly the text unit goes to the engine: {[c[:40] for c in eng.calls]}")
    by_id = {g.id: g for g in _segs(doc_id)}
    for g in tag_segs:
        g = by_id[g.id]
        if g.target_text != g.source_text or g.status != SegStatus.MT:
            _fail(f"[job] a tags-only unit must be copied as MT: {g.status} {g.target_text!r:.60}")
        if (g.meta or {}).get("mt_chunks"):
            _fail(f"[job] a copied unit is not chunked: {g.meta!r}")
    g = by_id[text_segs[0].id]
    if g.status != SegStatus.MT or not (g.target_text or "").startswith("Bonjour"):
        _fail(f"[job] the text unit must be translated: {g.status} {g.target_text!r}")
    res = job.result or {}
    if res.get("tags_only") != 2 or res.get("translated") != 3:
        _fail(f"[job] the summary must count the copies: {res!r}")
    with Session(engine) as s:
        out = export.build_translated_file(s, s.get(Document, doc_id), "fr-FR").decode("utf-8")
    if out.count('<x id="1699"/>') < 2 or "Bonjour Save the" not in out:
        _fail(f"[job] export must keep the tags-only target and the translation: {out[-400:]!r}")
    print("  [job] 2 tags-only units copied without an engine call, text unit translated OK")


def test_progress_total_first():
    doc_id = _upload("tags2.sdlxliff", _xliff())
    calls = []
    eng = _CountingEngine()
    saved = _install(eng)
    try:
        jobs._run_translate_document(
            {"document_id": doc_id, "locale": "fr-FR"},
            lambda done, total=None, message="": calls.append((done, total)),
        )
    finally:
        _restore(saved)
    if not calls or calls[0] != (0, 3):
        _fail(f"[progress] the total must be reported before the first segment: {calls[:3]}")
    print("  [progress] 0/3 reported before the first segment OK")


def test_helper():
    t = mt_chunking.tags_only
    assert t("⟦x1⟧"), "one tag"
    assert t("⟦x1⟧\n\t\t⟦x2⟧ "), "tags and whitespace"
    assert t("⟦g1⟧⟦/g1⟧"), "an empty paired tag"
    assert not t(""), "empty"
    assert not t("   "), "whitespace without a tag"
    assert not t("Save"), "text without a tag"
    assert not t("⟦x1⟧ Save ⟦x2⟧"), "a word"
    assert not t("⟦x1⟧ 2024 ⟦x2⟧"), "a number can be localised"
    assert not t("⟦x1⟧?⟦x2⟧"), "punctuation can be localised"
    assert not t("⟦x1⟧ ⟦not a tag⟧"), "a bracket run with spaces is not a tag"
    print("  [helper] tags_only OK")


if __name__ == "__main__":
    test_helper()
    test_job()
    test_progress_total_first()
    print("TAGS ONLY MT 702 OK")
