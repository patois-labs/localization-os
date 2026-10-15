"""The concept-editor feature-flag gate (SPEC-terminology-backend T1-lite).

Proves the DEFAULT-OFF `terminology_concept_editor` flag (app/modules.py) refuses
every concept/term-store WRITE seam on ALL FOUR surfaces, that a `.glossary.json`
import is refused into a populated glossary but allowed as a seed into an empty one,
and that turning the flag ON restores the write behaviour (so the OFF assertions are
non-vacuous). Reads stay available with the flag off.

  surfaces (flag OFF)   web POST routes (TestClient), the 7 /api concept write routes
                        (TestClient + token), the `glossary module concepts/terms`
                        CLI leaves (cli_main), and the edit_glossary MCP actions
                        (the real dispatcher, its httpx client pointed at the app via
                        ASGITransport) -- each refuses NAMING the switch.
  import                from_v1 via glossary.import_bytes: refused into a glossary that
                        holds entries/concepts (message names the empty condition),
                        allowed into an empty glossary that has >=1 language pair.
  reads                 GET /api/glossaries/{gid}/concepts stays 200 with the flag off.
  ON (non-vacuous)      with the flag on, create_concept + import-into-populated work.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). ASCII-only prints.
Final line: "GLOSSARY CONCEPT FLAG OK".
"""

import asyncio
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_glcflag_")

_MCP_DIR = os.path.join(_ROOT, "integrations", "mcp-server")
sys.path.insert(0, _MCP_DIR)

try:
    import server as mcp_server
except ModuleNotFoundError as exc:  # pragma: no cover -- only without the SDK
    if "mcp" in str(exc):
        # FAIL LOUDLY, never a green skip (the verify_mcp_tools discipline): this suite
        # proves the MCP surface of the kill switch, so without the SDK it must be RED.
        print(
            "  FATAL: the mcp SDK is not installed, so the MCP arm of the concept-editor "
            "flag gate cannot run. This suite MUST NOT be skipped. Install the mcp SDK.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    raise

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.services import apikeys, security, settings
from app.services import glossary as gloss_engine
from app.services import glossary_concepts as gc
from app.services import glossary_module as gm

init_db()

SRC, TGT = "en-US", "es-ES"
_FLAG = "terminology_concept_editor_enabled"
_SWITCH = "terminology_concept_editor"  # the substring every refusal must carry


def _fail(msg):
    raise AssertionError(msg)


def _set_flag(on: bool):
    settings.set_many({_FLAG: "1" if on else "0"})
    settings.invalidate_cache()


def _v1_doc():
    """A minimal valid glossary-concept/v1 document (one concept, two terms)."""
    return {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "seed-1",
                "definition": "a billing document",
                "terms": [
                    {"term": "invoice", "locale": SRC, "status": "preferred"},
                    {"term": "factura", "locale": TGT, "status": "preferred"},
                ],
            }
        ],
    }


# --------------------------------------------------------------------------- #
# fixtures (built with the flag ON, then the flag is flipped OFF for the sweep)
# --------------------------------------------------------------------------- #
def _seed():
    _set_flag(True)
    with Session(engine) as s:
        pop = gm.create(s, "flag-populated")
        pop_id = pop.id
        gm.set_pairs(s, pop_id, [f"{SRC}:{TGT}"])
        empty = gm.create(s, "flag-empty")
        empty_id = empty.id
        gm.set_pairs(s, empty_id, [f"{SRC}:{TGT}"])
    # a populated glossary: one concept, two terms (projects a compat entry)
    with Session(engine) as s:
        concept = gc.create_concept(
            s,
            pop_id,
            definition="a billing document",
            terms=[
                {"term": "invoice", "locale": SRC, "status": "preferred"},
                {"term": "factura", "locale": TGT, "status": "preferred"},
            ],
        )
        cid = concept.id
    with Session(engine) as s:
        from app.models import GlossaryTerm

        tid = s.exec(gc.select(GlossaryTerm).where(GlossaryTerm.concept_id == cid)).first().id
    return pop_id, empty_id, cid, tid


# --------------------------------------------------------------------------- #
# surface 1 -- web POST routes (TestClient; login wall off so no CSRF/auth)
# --------------------------------------------------------------------------- #
def check_web(pop_id, cid, tid):
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    c = TestClient(app)
    base = f"/glossaries/{pop_id}/concepts"
    posts = [
        ("add_concept", base, {"definition": "x"}),
        ("edit_concept", f"{base}/{cid}/edit", {"definition": "y"}),
        ("add_concept_term", f"{base}/{cid}/terms/add", {"term": "widget", "locale": SRC}),
        ("edit_concept_term", f"{base}/{cid}/terms/{tid}/edit", {"status": "admitted"}),
        ("set_audience", f"{base}/terms/audience", {"term_id": tid, "audience": "human"}),
        ("delete_concept_term", f"{base}/{cid}/terms/{tid}/delete", {}),
        ("delete_concept", f"{base}/{cid}/delete", {}),
    ]
    for label, url, data in posts:
        r = c.post(url, data=data, follow_redirects=False)
        assert r.status_code == 303, f"web {label}: expected 303, got {r.status_code}"
        loc = r.headers.get("location", "")
        assert _SWITCH in loc, f"web {label}: refusal did not name the switch: {loc!r}"
    print("  [1] web: all 7 concept-editor POST routes refuse naming the switch (303) OK")


# --------------------------------------------------------------------------- #
# surface 2 -- the 7 /api concept write routes (TestClient + token -> 400)
# --------------------------------------------------------------------------- #
def check_api(pop_id, cid, tid, token):
    c = TestClient(app)
    h = {"X-Loc-Token": token}
    base = f"/api/glossaries/{pop_id}/concepts"
    calls = [
        ("add_concept", "POST", base, {"definition": "x"}),
        ("edit_concept", "POST", f"{base}/{cid}/edit", {"definition": "y"}),
        ("add_concept_term", "POST", f"{base}/{cid}/terms", {"term": "widget", "locale": SRC}),
        ("edit_concept_term", "POST", f"{base}/{cid}/terms/{tid}/edit", {"status": "admitted"}),
        ("delete_concept_term", "POST", f"{base}/{cid}/terms/{tid}/delete", {}),
        (
            "set_audience",
            "POST",
            f"{base}/terms/audience",
            {"term_ids": [tid], "audience": "human"},
        ),
        ("delete_concept", "POST", f"{base}/{cid}/delete", {}),
    ]
    for label, _m, url, body in calls:
        r = c.post(url, json=body, headers=h)
        assert r.status_code == 400, f"/api {label}: expected 400, got {r.status_code} ({r.text})"
        err = (r.json() or {}).get("error", "")
        assert _SWITCH in err, f"/api {label}: refusal did not name the switch: {err!r}"
    # Reads stay available with the flag off.
    r = c.get(base, headers=h)
    assert r.status_code == 200, f"/api concepts READ 404'd with the flag off: {r.status_code}"
    print("  [2] /api: all 7 concept write routes 400 naming the switch; the READ stays 200 OK")


# --------------------------------------------------------------------------- #
# surface 3 -- the CLI leaves (cli_main) -- exercise the three that self-translate
# --------------------------------------------------------------------------- #
def _run_cli(argv):
    import io
    from contextlib import redirect_stderr, redirect_stdout

    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = cli_main(argv)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    return rc, buf.getvalue()


def check_cli(pop_id, cid, tid):
    gid = str(pop_id)
    probes = [
        ["glossary", "module", "concepts", "add", gid, "--definition", "x"],
        ["glossary", "module", "concepts", "edit", gid, str(cid), "--definition", "z"],
        ["glossary", "module", "concepts", "delete", gid, str(cid), "--yes"],
        ["glossary", "module", "terms", "add", gid, str(cid), "--term", "widget", "--locale", SRC],
        ["glossary", "module", "terms", "edit", gid, str(cid), str(tid), "--status", "admitted"],
        ["glossary", "module", "terms", "delete", gid, str(cid), str(tid), "--yes"],
        [
            "glossary",
            "module",
            "terms",
            "set-audience",
            gid,
            "--term",
            str(tid),
            "--audience",
            "human",
        ],
    ]
    for argv in probes:
        rc, out = _run_cli(argv)
        assert rc != 0, "CLI `{}` did not refuse (rc {})".format(" ".join(argv), rc)
        assert _SWITCH in out, "CLI `{}` refusal did not name the switch: {!r}".format(
            " ".join(argv), out[:200]
        )
    print(
        "  [3] CLI: all 7 concepts/terms add+edit+delete+set-audience refuse naming the switch OK"
    )


# --------------------------------------------------------------------------- #
# surface 4 -- the edit_glossary MCP actions (real dispatcher, app via ASGITransport)
# --------------------------------------------------------------------------- #
def check_mcp(pop_id, cid, tid, token):
    def _asgi_client():
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp")

    saved_client, saved_base, saved_token = (
        mcp_server._client,
        mcp_server.BASE_URL,
        mcp_server.TOKEN,
    )
    mcp_server._client = _asgi_client
    mcp_server.BASE_URL = "http://mcp"
    mcp_server.TOKEN = token
    try:
        actions = [
            {"action": "add_concept", "glossary_id": pop_id, "definition": "x"},
            {"action": "edit_concept", "glossary_id": pop_id, "concept_id": cid, "definition": "z"},
            {"action": "add_concept_term", "glossary_id": pop_id, "concept_id": cid, "term": "w"},
            {
                "action": "edit_concept_term",
                "glossary_id": pop_id,
                "concept_id": cid,
                "term_id": tid,
                "status": "admitted",
            },
            {
                "action": "delete_concept_term",
                "glossary_id": pop_id,
                "concept_id": cid,
                "term_id": tid,
            },
            {
                "action": "set_audience",
                "glossary_id": pop_id,
                "term_ids": [tid],
                "audience": "human",
            },
            {"action": "delete_concept", "glossary_id": pop_id, "concept_id": cid},
        ]
        for args in actions:
            try:
                asyncio.run(mcp_server.call_tool_unvalidated("edit_glossary", args))
            except mcp_server.ToolError as exc:
                assert _SWITCH in str(exc), (
                    f"MCP {args['action']}: refusal did not name the switch: {str(exc)!r}"
                )
                continue
            _fail(f"MCP {args['action']} did not refuse while the flag is off")
    finally:
        mcp_server._client = saved_client
        mcp_server.BASE_URL = saved_base
        mcp_server.TOKEN = saved_token
    print("  [4] MCP: all 7 edit_glossary concept/term actions relay the switch refusal OK")


# --------------------------------------------------------------------------- #
# import: refused into a populated glossary, allowed into an empty paired one
# --------------------------------------------------------------------------- #
def check_import(pop_id, empty_id):
    """A.7: after the authority inversion the concept-preserving import is UNGATED on every
    install (from_v1/import_concepts_batched are safe -- they upsert only the NAMED concepts
    and derive the compat rows by projection). A populated import is ALLOWED and touches only
    named concepts; the T1-lite empty-only refusal is gone (the retired check is REPLACED)."""
    from app.models import GlossaryConcept

    doc_bytes = json.dumps(_v1_doc()).encode("utf-8")
    # (a) into a POPULATED glossary -> allowed (ungated); reconciles the named concept by
    # content identity rather than minting a duplicate (touches only named concepts).
    with Session(engine) as s:
        n_before = len(
            s.exec(gc.select(GlossaryConcept).where(GlossaryConcept.glossary_id == pop_id)).all()
        )
        res = gloss_engine.import_bytes(
            s, None, doc_bytes, SRC, TGT, glossary_id=pop_id, filename="x.glossary.json"
        )
        assert res.get("concepts", 0) >= 1, f"import-into-populated wrote no concept: {res!r}"
    with Session(engine) as s:
        n_after = len(
            s.exec(gc.select(GlossaryConcept).where(GlossaryConcept.glossary_id == pop_id)).all()
        )
        assert n_after == n_before, (
            f"import-into-populated minted a duplicate concept ({n_before} -> {n_after}); "
            "it must reconcile the named concept by content identity"
        )
    # (b) into an empty glossary that has a language pair -> allowed (the seed path)
    with Session(engine) as s:
        res = gloss_engine.import_bytes(
            s, None, doc_bytes, SRC, TGT, glossary_id=empty_id, filename="x.glossary.json"
        )
        assert res.get("concepts", 0) >= 1, f"import-into-empty seeded no concept: {res!r}"
    with Session(engine) as s:
        doc = gc.list_concepts(s, empty_id)
        assert doc.get("concepts"), "import-into-empty left the glossary with no concepts"
    print(
        "  [5] import: .glossary.json is UNGATED (A.7) -- allowed into a populated glossary "
        "(reconciles by content identity, no duplicate) and as a seed into an empty one OK"
    )


# --------------------------------------------------------------------------- #
# non-vacuous: with the flag ON the writes work again
# --------------------------------------------------------------------------- #
def check_on(empty_id):
    _set_flag(True)
    with Session(engine) as s:
        concept = gc.create_concept(s, empty_id, definition="on-check")
        assert concept.id is not None, "create_concept did not succeed with the flag on"
    # import into the now-populated glossary is accepted with the flag on
    doc_bytes = json.dumps(_v1_doc()).encode("utf-8")
    with Session(engine) as s:
        res = gloss_engine.import_bytes(
            s, None, doc_bytes, SRC, TGT, glossary_id=empty_id, filename="x.glossary.json"
        )
        assert res.get("format") == gloss_engine.JSON_FORMAT, (
            f"import-into-populated (flag ON) was not accepted: {res!r}"
        )
    print("  [6] flag ON: create_concept + .glossary.json import into a populated glossary work OK")


# --------------------------------------------------------------------------- #
# LT-2: the web concept view is READ-ONLY with the flag off (no write controls)
# --------------------------------------------------------------------------- #
def check_web_readonly(pop_id, cid):
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    c = TestClient(app)
    r = c.get(f"/glossaries/{pop_id}", follow_redirects=False)
    assert r.status_code == 200, f"glossary detail not 200 with the flag off: {r.status_code}"
    html = r.text
    forbidden = [
        f'action="/glossaries/{pop_id}/concepts/{cid}/edit"',
        f'action="/glossaries/{pop_id}/concepts/{cid}/delete"',
        f'action="/glossaries/{pop_id}/concepts"',  # the "Add a concept" form
        f"/glossaries/{pop_id}/concepts/{cid}/terms/",  # the per-term x delete form
    ]
    for frag in forbidden:
        assert frag not in html, f"flag OFF: concept write control still rendered: {frag!r}"
    assert "Concept editing is off" in html, "flag OFF: the read-only note is missing"
    # non-vacuous: with the flag ON the edit control IS rendered again.
    _set_flag(True)
    on_html = c.get(f"/glossaries/{pop_id}", follow_redirects=False).text
    assert f'action="/glossaries/{pop_id}/concepts/{cid}/edit"' in on_html, (
        "flag ON: the concept edit control should render (the OFF sweep would be vacuous)"
    )
    _set_flag(False)
    print(
        "  [7] web view: read-only with the flag off (no write controls, note shown); ON restores"
    )


# --------------------------------------------------------------------------- #
# LT-1: a legacy target edit retires the replaced target, so the export carries one
# preferred target per pair and a CLI seed import reproduces the CURRENT target.
# --------------------------------------------------------------------------- #
def _mkg(name):
    with Session(engine) as s:
        g = gm.create(s, name)
        gid = g.id
        gm.set_pairs(s, gid, [f"{SRC}:{TGT}"])
    return gid


def _es_preferred(doc):
    return [
        t.get("term")
        for c in doc.get("concepts", [])
        for t in c.get("terms", [])
        if (t.get("locale") or "") == TGT and t.get("status") == "preferred"
    ]


def _entry_targets(gid):
    from app.models import GlossaryEntry

    with Session(engine) as s:
        return sorted(
            (e.source_term, e.target_term)
            for e in s.exec(gc.select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
        )


def check_lt1_seed(tmp_dir):
    # Source glossary: add then edit the target TWICE through the legacy seams (ungated).
    src_g = _mkg("lt1-source")
    with Session(engine) as s:
        e = gloss_engine.add_term(s, None, "invoice", "factura", SRC, TGT, glossary_id=src_g)
        eid = e.id
    with Session(engine) as s:
        gloss_engine.update_entry(s, eid, target_term="recibo")
    with Session(engine) as s:
        gloss_engine.update_entry(s, eid, target_term="comprobante")

    # The export carries exactly ONE preferred es target -- the current one.
    with Session(engine) as s:
        src_doc = gc.to_v1(s, src_g)
    _all_es = [
        (t.get("term"), t.get("status"))
        for cc in src_doc.get("concepts", [])
        for t in cc.get("terms", [])
        if (t.get("locale") or "") == TGT
    ]
    assert _es_preferred(src_doc) == ["comprobante"], (
        "after two legacy edits the export must carry one preferred es target, got "
        f"{_es_preferred(src_doc)!r} (all es: {_all_es!r})"
    )

    # Export + import THROUGH THE CLI (not the raw seam) into a fresh empty paired glossary.
    dst_g = _mkg("lt1-seed")
    out_path = os.path.join(tmp_dir, "lt1.glossary.json")
    rc, out = _run_cli(["glossary", "module", "export", str(src_g), out_path, "--force"])
    assert rc == 0, f"CLI module export failed rc={rc}: {out[:300]!r}"
    rc, out = _run_cli(
        ["glossary", "module", "import", str(dst_g), out_path, "--source", SRC, "--target", TGT]
    )
    assert rc == 0, f"CLI module import failed rc={rc}: {out[:300]!r}"

    # The seed reproduces the CURRENT target, not the oldest.
    assert _entry_targets(dst_g) == [("invoice", "comprobante")], (
        f"seed import did not reproduce the current target: {_entry_targets(dst_g)!r}"
    )
    with Session(engine) as s:
        m_src = gloss_engine.match_terms(s, None, "the invoice", SRC, TGT, glossary_id=src_g)
        m_dst = gloss_engine.match_terms(s, None, "the invoice", SRC, TGT, glossary_id=dst_g)
    tgt_src = [m.get("target_term") for m in m_src]
    tgt_dst = [m.get("target_term") for m in m_dst]
    assert tgt_src == ["comprobante"] == tgt_dst, (
        f"match_terms parity failed: source {tgt_src!r} vs seed {tgt_dst!r}"
    )
    with Session(engine) as s:
        dst_doc = gc.to_v1(s, dst_g)
    assert _es_preferred(dst_doc) == ["comprobante"], (
        "the seeded glossary's export must also carry one preferred es target"
    )
    print(
        "  [8] LT-1: two legacy target edits retire the old target; the CLI export carries one "
        "preferred target and a CLI seed import reproduces the CURRENT target (match_terms parity)"
    )


def check_lt1_csv(tmp_dir):
    # The same class through a CSV re-import (_apply_rows), driven via the CLI.
    g = _mkg("lt1-csv")
    csv1 = os.path.join(tmp_dir, "v1.csv")
    csv2 = os.path.join(tmp_dir, "v2.csv")
    with open(csv1, "w", encoding="utf-8") as fh:
        fh.write("source_term,target_term,dnt\ninvoice,factura,\n")
    with open(csv2, "w", encoding="utf-8") as fh:
        fh.write("source_term,target_term,dnt\ninvoice,recibo,\n")
    for path in (csv1, csv2):
        rc, out = _run_cli(
            ["glossary", "module", "import", str(g), path, "--source", SRC, "--target", TGT]
        )
        assert rc == 0, f"CLI CSV import failed rc={rc}: {out[:300]!r}"
    with Session(engine) as s:
        doc = gc.to_v1(s, g)
    assert _es_preferred(doc) == ["recibo"], (
        f"a CSV re-import that moved the target left a stale preferred synonym: {_es_preferred(doc)!r}"  # noqa: E501
    )
    assert _entry_targets(g) == [("invoice", "recibo")], _entry_targets(g)
    print("  [9] LT-1: a CSV re-import that moves a target retires the old one (one preferred) OK")


# --------------------------------------------------------------------------- #
# LT2-1 / LT2-2 / LT2-4: retire must not touch a still-referenced term, must compare
# surfaces verbatim, and remove-term must not drop a live base concept. All are on the
# ungated legacy seams (flag-independent) -- exercised here with the flag OFF, plus a
# flag-ON negative control that they behave identically.
# --------------------------------------------------------------------------- #
def _mkg2(name, pairs):
    with Session(engine) as s:
        g = gm.create(s, name)
        gid = g.id
        gm.set_pairs(s, gid, pairs)
    return gid


def _all_status(gid):
    from app.models import GlossaryConcept, GlossaryTerm

    with Session(engine) as s:
        cids = set(
            s.exec(gc.select(GlossaryConcept.id).where(GlossaryConcept.glossary_id == gid)).all()
        )
        return sorted(
            (t.locale or "", t.term, t.status)
            for t in s.exec(gc.select(GlossaryTerm)).all()
            if t.concept_id in cids
        )


def _has_deprecated(doc):
    return any(t.get("status") == "deprecated" for c in doc.get("concepts", []) for t in c["terms"])


def _full_entries(gid):
    from app.models import GlossaryEntry

    with Session(engine) as s:
        return sorted(
            (e.source_locale, e.source_term, e.target_locale, e.target_term)
            for e in s.exec(gc.select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
        )


def _reversed_doc():
    return {
        "schema": "glossary-concept/v1",
        "concepts": [
            {
                "external_id": "rc1",
                "terms": [
                    {"term": "invoice", "locale": SRC, "status": "preferred"},
                    {"term": "factura", "locale": TGT, "status": "preferred"},
                ],
            }
        ],
    }


def check_lt21_reversed(tag=""):
    """LT2-1: on a two-direction (reversed) pair glossary, editing the reversed row's target must
    NOT deprecate/delete the concept's live SOURCE surface -- else `to_v1` exports it `deprecated`
    and a restore loses the FORWARD pair's whole enforced entry."""
    from app.models import GlossaryEntry

    g = _mkg2("lt21-rev" + tag, [f"{SRC}:{TGT}", f"{TGT}:{SRC}"])
    with Session(engine) as s:
        gc.from_v1(s, g, _reversed_doc())  # flag-OFF-permitted seed into an empty paired glossary
        rev_id = (
            s.exec(
                gc.select(GlossaryEntry).where(
                    GlossaryEntry.glossary_id == g,
                    GlossaryEntry.source_locale == TGT,
                    GlossaryEntry.target_locale == SRC,
                )
            )
            .first()
            .id
        )
    rc, out = _run_cli(
        ["glossary", "module", "edit-term", str(g), str(rev_id), "--target-term", "bill"]
    )
    assert rc == 0, f"edit-term on the reversed row failed rc={rc}: {out[:200]!r}"
    with Session(engine) as s:
        doc = gc.to_v1(s, g)
    assert not _has_deprecated(doc), (
        f"a reversed-pair edit deprecated a live source surface: {_all_status(g)!r}"
    )
    # the forward entry survives a re-seed and still matches (the regression LOST it).
    h = _mkg2("lt21-rev-seed" + tag, [f"{SRC}:{TGT}", f"{TGT}:{SRC}"])
    with Session(engine) as s:
        gc.from_v1(s, h, doc)
    assert (SRC, "invoice", TGT, "factura") in _full_entries(h), (
        f"the reversed-pair edit lost invoice->factura on restore: {_full_entries(h)!r}"
    )
    with Session(engine) as s:
        m_src = [
            m["target_term"]
            for m in gloss_engine.match_terms(s, None, "an invoice", SRC, TGT, glossary_id=g)
        ]
        m_seed = [
            m["target_term"]
            for m in gloss_engine.match_terms(s, None, "an invoice", SRC, TGT, glossary_id=h)
        ]
    assert m_src == ["factura"] == m_seed, (
        f"reversed-pair match parity failed: {m_src!r} vs seed {m_seed!r}"
    )


def check_lt21_self_target():
    """LT2-1 self-target (X->X DNT, source == target surface): editing the target must not
    deprecate/delete the source surface; a later remove-term leaves no deprecated residue."""
    from app.models import GlossaryEntry

    g = _mkg2("lt21-self", [f"{SRC}:{SRC}"])
    with Session(engine) as s:
        eid = gloss_engine.add_term(
            s, None, "Widget", "Widget", SRC, SRC, glossary_id=g, dnt=True
        ).id
    rc, out = _run_cli(
        ["glossary", "module", "edit-term", str(g), str(eid), "--target-term", "Gadget"]
    )
    assert rc == 0, f"self-target edit-term failed rc={rc}: {out[:200]!r}"
    with Session(engine) as s:
        doc = gc.to_v1(s, g)
    assert not _has_deprecated(doc), (
        f"a self-target edit deprecated the source surface: {_all_status(g)!r}"
    )
    h = _mkg2("lt21-self-seed", [f"{SRC}:{SRC}"])
    with Session(engine) as s:
        gc.from_v1(s, h, doc)
    assert (SRC, "Widget", SRC, "Gadget") in _full_entries(h), (
        f"the self-target edit lost Widget->Gadget on restore: {_full_entries(h)!r}"
    )
    with Session(engine) as s:
        e2 = s.exec(gc.select(GlossaryEntry).where(GlossaryEntry.glossary_id == g)).first()
        gm.remove_term(s, g, e2.id)
        s.commit()
    assert not any(st == "deprecated" for _, _, st in _all_status(g)), (
        f"remove-term left a deprecated residue: {_all_status(g)!r}"
    )
    print(
        "  [11] LT2-1: reversed-pair + self-target edit-term never deprecate the live source "
        "surface; forward entry + match survive a re-seed; remove-term leaves no residue OK"
    )


def check_lt22_case_only(tag=""):
    """LT2-2: a case-only target correction IS a surface change -- one preferred es term, and a
    seed restores the CORRECTED casing (not the pre-edit one)."""
    g = _mkg2("lt22-case" + tag, [f"{SRC}:{TGT}"])
    with Session(engine) as s:
        eid = gloss_engine.add_term(s, None, "brand", "iPhone", SRC, TGT, glossary_id=g).id
    rc, out = _run_cli(
        ["glossary", "module", "edit-term", str(g), str(eid), "--target-term", "iphone"]
    )
    assert rc == 0, f"case-only edit-term failed rc={rc}: {out[:200]!r}"
    with Session(engine) as s:
        doc = gc.to_v1(s, g)
    _es = [
        (t["term"], t.get("status"))
        for c in doc["concepts"]
        for t in c["terms"]
        if t.get("locale") == TGT
    ]
    assert _es_preferred(doc) == ["iphone"], (
        f"a case-only edit left both casings preferred: {_es!r}"
    )
    h = _mkg2("lt22-case-seed" + tag, [f"{SRC}:{TGT}"])
    with Session(engine) as s:
        gc.from_v1(s, h, doc)
    assert _entry_targets(h) == [("brand", "iphone")], (
        f"the case-only edit did not restore the corrected casing: {_entry_targets(h)!r}"
    )
    if not tag:
        print(
            "  [12] LT2-2: a case-only target correction (iPhone->iphone) retires the old casing "
            "(one preferred; a seed restores 'iphone') OK"
        )


def check_lt24_remove_term():
    """LT2-4: two BASE concepts sharing a source SURFACE across different source locales land in
    one pair-/locale-free homonym group; remove-term must not treat a live base concept as a
    disposable sibling (was a FK IntegrityError 500 under PRAGMA foreign_keys=ON)."""
    g = _mkg2("lt24", [f"{SRC}:{TGT}", f"{TGT}:{SRC}"])
    with Session(engine) as s:
        e1 = gloss_engine.add_term(s, None, "invoice", "factura", SRC, TGT, glossary_id=g)
        gm.add_sense(s, g, e1.id, "billing", "factura", "money")
    with Session(engine) as s:
        e2 = gloss_engine.add_term(s, None, "invoice", "bill", TGT, SRC, glossary_id=g)
        gm.add_sense(s, g, e2.id, "request", "bill", "ask")
        e2_id = e2.id
    with Session(engine) as s:
        ok = gm.remove_term(s, g, e2_id)  # must not raise IntegrityError
        s.commit()
    assert ok is True, "remove_term did not return True"
    assert (SRC, "invoice", TGT, "factura") in _full_entries(g), (
        f"remove-term destroyed the surviving base concept's row: {_full_entries(g)!r}"
    )
    print(
        "  [13] LT2-4: remove-term on a locale-free homonym-group shape returns rc 0 (no FK-500); "
        "the live base concept's row survives OK"
    )


def check_lt2_flag_on_control():
    """Negative control: the LT2-1/LT2-2 retire fixes are on the UNGATED legacy seams, so they
    behave identically with the flag ON -- re-run the reversed-pair + case-only with it on."""
    _set_flag(True)
    try:
        check_lt21_reversed(tag="-on")
        check_lt22_case_only(tag="-on")
    finally:
        _set_flag(False)
    print(
        "  [14] LT2 flag-ON control: reversed-pair + case-only retire behave identically with "
        "the editor ON (the fix is flag-independent, so the OFF assertions are non-vacuous) OK"
    )


def main():
    import tempfile

    pop_id, empty_id, cid, tid = _seed()
    security.seed_initial()
    admin = security.get_by_email("admin")
    # docs scope: the concept write routes require a document-scoped key (can_tune_ai).
    _row, token = apikeys.create_key(admin.id, "concept-flag", scopes="api docs")

    _set_flag(False)  # the sweep runs with the editor OFF
    check_web(pop_id, cid, tid)
    check_api(pop_id, cid, tid, token)
    check_cli(pop_id, cid, tid)
    check_mcp(pop_id, cid, tid, token)
    check_import(pop_id, empty_id)
    check_web_readonly(pop_id, cid)  # LT-2 (leaves the flag OFF)
    tmp_dir = tempfile.mkdtemp(prefix="loctms_glcflag_lt1_")
    check_lt1_seed(tmp_dir)  # LT-1 (legacy seams are ungated -- run with the flag OFF)
    check_lt1_csv(tmp_dir)  # LT-1 CSV re-import variant
    check_lt21_reversed()  # LT2-1 reversed pair
    check_lt21_self_target()  # LT2-1 self-target + remove-term residue
    check_lt22_case_only()  # LT2-2 case-only correction
    check_lt2_flag_on_control()  # flag-ON negative control for LT2-1 / LT2-2
    check_lt24_remove_term()  # LT2-4 remove-term FK shape
    check_on(empty_id)
    print("GLOSSARY CONCEPT FLAG OK")


if __name__ == "__main__":
    main()
