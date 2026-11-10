"""A Markdown paragraph that holds an image is translated in FULL-TEXT mode (GitHub #667).

  rule       `a11y.full_text_units`: rule 2 keeps the in-body style `a11y:md-image` (a prose
             line that holds an image) and still leaves out appended units (image alt and
             link title attributes, page metadata); rule 1 (no stamp) leaves them all out;
             a missing, unknown or non-integer stamp reads as rule 1
  new        a fresh full-text upload keeps the image paragraph in its one body (stamp
             ft_units = 2), its prose and its alt text are translated as in segmented mode
             and the `(...)` target is kept; segmented mode is unchanged (3 segments, the
             image line carries its Editor note); an HTML full-text body still leaves its
             alt attributes out
  old        a full-text document imported before #667 (no stamp, the image paragraph not
             in its body) exports byte-identically to before, and a repair
             (reingest_document) keeps its two paragraphs and no stamp, so the stored
             two-paragraph translation still exports; a new document's repair keeps rule 2
  e2e        API upload (mode full_text), the stored translation, API download

Hermetic + offline. Final line: "MD FULLTEXT IMAGE 667 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_md667_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.filters import a11y
from app.models import Document, Project, Segment, TargetLocale
from app.services import export, ingest

init_db()

_TEST_PW = "md667-pw-1234567"  # pregate: allow-secret throwaway test user

SRC = b"# Title\n\nHello world see ![cat](cat.png) here and more words.\n\nLast para.\n"
IMG = "Hello world see ![cat](cat.png) here and more words."
IMG_FR = "Bonjour le monde voir ![chat](cat.png) ici et plus de mots."
# What the pre-#667 code stored and exported for SRC in full-text mode (build/issues-1107,
# f3ae584fd): the image paragraph is left out of the body and stays English.
OLD_BODY = "Title\n\nLast para."
OLD_EXPORT = b"# Titre\n\nHello world see ![cat](cat.png) here and more words.\n\nDernier para.\n"
NEW_EXPORT = (
    b"# Titre\n\nBonjour le monde voir ![chat](cat.png) ici et plus de mots.\n\nDernier para.\n"
)


def _project(name: str) -> int:
    with Session(engine) as s:
        p = Project(name=name, org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        return p.id


def _ingest(pid: int, name: str, data: bytes, mode: str) -> int:
    with Session(engine) as s:
        return ingest.ingest_document(s, s.get(Project, pid), name, data, mode).id


def _segs(did: int) -> list:
    with Session(engine) as s:
        rows = s.exec(
            select(Segment).where(Segment.document_id == did, Segment.target_locale == "fr-FR")
        ).all()
        return sorted(rows, key=lambda r: r.order_index)


def _set_target(did: int, texts: list[str]) -> None:
    with Session(engine) as s:
        rows = s.exec(
            select(Segment).where(Segment.document_id == did, Segment.target_locale == "fr-FR")
        ).all()
        rows = sorted(rows, key=lambda r: r.order_index)
        assert len(rows) == len(texts), ([r.source_text for r in rows], texts)
        for r, t in zip(rows, texts, strict=True):
            r.target_text = t
            r.status = "translated"
            s.add(r)
        s.commit()


def _clear_targets(did: int) -> None:
    with Session(engine) as s:
        for r in s.exec(select(Segment).where(Segment.document_id == did)).all():
            r.target_text = ""
            r.status = "new"
            s.add(r)
        s.commit()


def _export(did: int) -> bytes:
    with Session(engine) as s:
        return export.build_translated_file(s, s.get(Document, did), "fr-FR")


def _make_old(did: int) -> None:
    """Turn a fresh full-text document into exactly what the pre-#667 ingest stored: no
    ft_units stamp, every `a11y:*` unit left out of the units and of the one body."""
    from app import config
    from app.services.segment import est_tokens, plan_chunks

    with Session(engine) as s:
        doc = s.get(Document, did)
        skel = dict(doc.skeleton)
        skel.pop("ft_units")
        units = [u for u in skel["units"] if not a11y.is_appended_style(u.get("style"))]
        skel["units"] = units
        texts = [u["text"] for u in units]
        skel["chunk_plan"] = plan_chunks(texts, config.MAX_CHUNK_TOKENS)
        skel["est_tokens_total"] = sum(est_tokens(t) for t in texts)
        doc.skeleton = skel
        s.add(doc)
        for r in s.exec(select(Segment).where(Segment.document_id == did)).all():
            r.source_text = "\n\n".join(texts)
            s.add(r)
        s.commit()


def test_rule():
    units = [
        {"p_index": 0, "text": "Title", "style": ""},
        {"p_index": 1, "text": IMG, "style": "a11y:md-image"},
        {"p_index": 2, "text": "cat", "style": "a11y:img-alt"},
        {"p_index": 3, "text": "Tip", "style": "a11y:link-title"},
        {"p_index": 4, "text": "About", "style": "meta:description"},
        {"p_index": 5, "text": "Plain", "style": None},
    ]
    keep2 = [u["p_index"] for u in a11y.full_text_units(units, a11y.FT_UNITS_VERSION)]
    keep1 = [u["p_index"] for u in a11y.full_text_units(units, a11y.FT_UNITS_LEGACY)]
    assert keep2 == [0, 1, 5], keep2
    assert keep1 == [0, 5], keep1
    assert a11y.FT_UNITS_VERSION == 2 and a11y.FT_UNITS_LEGACY == 1
    for sk in (None, {}, {"ft_units": None}, {"ft_units": "2"}, {"ft_units": True}):
        assert a11y.ft_units_version(sk) == 1, sk
    assert a11y.ft_units_version({"ft_units": 99}) == 1  # unknown: the legacy rule
    assert a11y.ft_units_version({"ft_units": 2}) == 2
    # the legacy predicate itself is unchanged (HubSpot body counts, rule 1)
    assert a11y.is_appended_style("a11y:md-image") and a11y.is_appended_style("meta:keywords")
    print("  [rule] rule 2 keeps a11y:md-image, rule 1 and bad stamps leave it out OK")


def test_new_document():
    pid = _project("md667-new")
    did = _ingest(pid, "a.md", SRC, "full_text")
    segs = _segs(did)
    assert [g.source_text for g in segs] == [f"Title\n\n{IMG}\n\nLast para."], [
        g.source_text for g in segs
    ]
    with Session(engine) as s:
        skel = s.get(Document, did).skeleton
    assert skel.get("ft_units") == 2, skel.get("ft_units")
    assert [u["p_index"] for u in skel["units"]] == [0, 1, 2], skel["units"]
    _set_target(did, [f"Titre\n\n{IMG_FR}\n\nDernier para."])
    assert _export(did) == NEW_EXPORT, _export(did)
    # a repair of a new document keeps rule 2 (and its stamp)
    _clear_targets(did)
    assert ingest.reingest_document(did)
    with Session(engine) as s:
        assert s.get(Document, did).skeleton["ft_units"] == 2
    assert [g.source_text for g in _segs(did)] == [f"Title\n\n{IMG}\n\nLast para."]
    # segmented mode: as before (one segment per line, the image line has its note, no stamp)
    sid = _ingest(pid, "b.md", SRC, "segmented")
    segs = _segs(sid)
    assert [g.source_text for g in segs] == ["Title", IMG, "Last para."], segs
    assert (segs[1].meta or {}).get("notes") == a11y.unit_note("a11y:md-image"), segs[1].meta
    with Session(engine) as s:
        assert "ft_units" not in s.get(Document, sid).skeleton
    _set_target(sid, ["Titre", IMG_FR, "Dernier para."])
    assert _export(sid) == NEW_EXPORT, _export(sid)
    # an image-only paragraph is a unit in full-text mode too (its alt text is the words)
    did2 = _ingest(pid, "c.md", b"Intro.\n\n![A red cat](cat.png)\n", "full_text")
    assert [g.source_text for g in _segs(did2)] == ["Intro.\n\n![A red cat](cat.png)"]
    # HTML full-text bodies still leave the appended alt attribute units out
    hid = _ingest(pid, "p.html", b'<p>Hello <img src="c.png" alt="A cat"> there.</p>', "full_text")
    with Session(engine) as s:
        hs = s.get(Document, hid).skeleton
    assert not any(a11y.is_appended_style(u.get("style")) for u in hs["units"]), hs["units"]
    assert all("A cat" not in g.source_text for g in _segs(hid)), _segs(hid)
    print("  [new] full-text body keeps the image paragraph, export translates it OK")


def test_old_document():
    pid = _project("md667-old")
    did = _ingest(pid, "old.md", SRC, "full_text")
    _make_old(did)
    with Session(engine) as s:
        skel = s.get(Document, did).skeleton
    assert "ft_units" not in skel and [u["p_index"] for u in skel["units"]] == [0, 2]
    assert [g.source_text for g in _segs(did)] == [OLD_BODY]
    # export: the stored two-paragraph translation exports exactly as before #667
    _set_target(did, ["Titre\n\nDernier para."])
    assert _export(did) == OLD_EXPORT, _export(did)
    # repair: the unstamped document keeps rule 1, its body and its unit list
    _clear_targets(did)
    assert ingest.reingest_document(did)
    with Session(engine) as s:
        skel = s.get(Document, did).skeleton
    assert "ft_units" not in skel, skel.get("ft_units")
    assert [u["p_index"] for u in skel["units"]] == [0, 2], skel["units"]
    assert [g.source_text for g in _segs(did)] == [OLD_BODY], _segs(did)
    # and a two-paragraph translation of the repaired document still exports
    _set_target(did, ["Titre\n\nDernier para."])
    assert _export(did) == OLD_EXPORT, _export(did)
    print("  [old] unstamped document: export and repair unchanged OK")


def test_end_to_end():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Role
    from app.services import apikeys, security

    pid = _project("md667-e2e")
    user = security.create_user("md667@x.test", "md667", Role.ADMIN, _TEST_PW)
    _row, raw = apikeys.create_key(user.id, "md667", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid), "mode": "full_text"},
        files={"file": ("readme.md", SRC, "text/markdown")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    did = r.json()["document_id"]
    assert [g.source_text for g in _segs(did)] == [f"Title\n\n{IMG}\n\nLast para."]
    _set_target(did, [f"Titre\n\n{IMG_FR}\n\nDernier para."])
    r = client.get(f"/api/documents/{did}/download", headers=hdr, params={"locale": "fr-FR"})
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.content == NEW_EXPORT, r.content
    print("  [e2e] API full-text upload and download translate the image paragraph OK")


def main():
    test_rule()
    test_new_document()
    test_old_document()
    test_end_to_end()
    print("MD FULLTEXT IMAGE 667 OK")


if __name__ == "__main__":
    main()
