"""The leverage report: 101 % / repetitions / 100 % / new, in words, per target language (ADR 0102).

app/services/leverage.py is the ONE seam. This suite pins:

  1. precedence on filled segments: 101 beats a repetition, the 2nd occurrence of a 100 is a
     repetition, an old 85 stamp is new, a 'fuzzy' lock is new, context locks count as 101;
  2. unfilled segments are looked up live (one batched lookup per document and language), with
     the segment's context key: same neighbours -> 101, other neighbours -> 100, nothing -> new;
  3. a key project gets 101 from the string key, and key content is part of the project scope;
  4. languages never share repetitions (the same source in de and fr is not a repetition);
  5. job scope (the document and language one translate job covered) versus project scope, and a
     job that covers no documents is a clear 400;
  6. the CSV shape, and the same numbers on API, CLI (local and --remote) and MCP;
  7. the vendor rate card: fuzzy_* keys are refused on save and tolerated on load, a persisted
     estimate with fuzzy cells folds into new;
  8. confinement: another org's project or job is a 404;
  9. speed: 5,000 segments.

ASCII-only output.
"""

import asyncio
import csv
import io
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_leverage_")

FIXTURE_PW = "fixture-pass-1234!!"  # pregate: allow-secret test fixture

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db

init_db()
from app.main import app
from app.models import (
    Customer,
    Document,
    Job,
    JobStatus,
    Organization,
    Project,
    Role,
    Segment,
    StringKey,
    User,
    Vendor,
)
from app.services import apikeys, leverage, security, settings, tm, tm_common, vendor_cost, vendors

SRC = "en-US"
DE, FR = "de-DE", "fr-FR"
_N = [0]


def _seq():
    _N[0] += 1
    return _N[0]


def mkproject(name, targets=DE, org_id=None):
    with Session(engine) as s:
        cust = Customer(name=f"{name} Co {_seq()}", org_id=org_id)
        s.add(cust)
        s.commit()
        s.refresh(cust)
        proj = Project(
            name=f"{name} {_seq()}",
            customer_id=cust.id,
            source_locale=SRC,
            target_locales=targets,
            org_id=org_id,
        )
        s.add(proj)
        s.commit()
        s.refresh(proj)
        return proj.id


def mkdoc(pid, rows, locale=DE, fmt="txt", filename=None):
    """rows: (source, target, tm_match_pct, lock_band) tuples, in order. Returns the doc id."""
    with Session(engine) as s:
        d = Document(project_id=pid, filename=filename or f"d{_seq()}.txt", fmt=fmt)
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, (src, tgt, pct, lock) in enumerate(rows):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale=locale,
                    order_index=i,
                    source_text=src,
                    target_text=tgt,
                    tm_match_pct=pct,
                    lock_band=lock,
                )
            )
        s.commit()
        return d.id


def bands(report, locale=None):
    """{band: (segments, words)} for one language, or the totals."""
    if locale is None:
        cells = report["totals"]["bands"]
    else:
        cells = next(x for x in report["languages"] if x["locale"] == locale)["bands"]
    return {b: (cells[b]["segments"], cells[b]["words"]) for b in leverage.BANDS}


def run_analyze(pid, **kw):
    with Session(engine) as s:
        return leverage.analyze(s, pid, **kw)


# ---------------------------------------------------------------------------
def test_vocabulary_and_precedence_on_filled_segments():
    assert leverage.BANDS == ("context", "repetitions", "exact", "new")
    assert vendor_cost.BANDS == leverage.BANDS
    assert [vendor_cost.BAND_LABELS[b] for b in vendor_cost.BANDS] == [
        "101 % in-context",
        "Repetitions",
        "100 % exact",
        "New",
    ]
    pid = mkproject("prec")
    mkdoc(
        pid,
        [
            ("alpha one two", "t", 100, None),  # first 100 -> exact
            ("alpha one two", "t", 100, None),  # 2nd occurrence of a 100 -> repetitions
            ("beta three four", "t", 85, None),  # old fuzzy stamp -> new
            ("gamma five six", "t", None, "fuzzy"),  # fuzzy lock -> new
            ("delta seven eight", "t", None, "context"),  # context lock -> context
            ("delta seven eight", "t", 101, None),  # 101 beats the repetition
            ("eps nine ten", "t", None, None),  # MT -> new
            ("eps  nine ten", "t", 100, None),  # whitespace-normalized repeat -> repetitions
            ("zeta eleven twelve", "t", None, "ice"),  # ice lock -> context
            ("eta thirteen fourteen", "t", None, "100"),  # 100 lock -> exact
        ],
    )
    r = run_analyze(pid)
    assert bands(r, DE) == {
        "context": (3, 9),
        "repetitions": (2, 6),
        "exact": (2, 6),
        "new": (3, 9),
    }, bands(r, DE)
    assert r["totals"]["segments"] == 10 and r["totals"]["words"] == 30
    assert bands(r) == bands(r, DE)
    assert list(r["languages"][0]["bands"]) == ["context", "repetitions", "exact", "new"]
    assert r["bands"] == ["context", "repetitions", "exact", "new"]
    assert r["scope"] == "project" and r["job_id"] is None and r["project_id"] == pid
    # classify_band (vendor dispatch pricing) follows the same rule from persisted fields
    seen: set = set()
    got = []
    for src, pct, lock in [
        ("a b", 100, None),
        ("a b", 100, None),
        ("c d", 85, None),
        ("e f", None, "fuzzy"),
        ("g h", None, "ice"),
        ("g h", 101, None),
    ]:
        got.append(vendor_cost.classify_band(src, pct, lock, seen))
        seen.add(src)
    assert got == ["exact", "repetitions", "new", "new", "context", "context"], got
    print(" 1 precedence: 101 > repetitions > 100 > new; old 85 and fuzzy lock are new")


def test_live_lookup_with_context():
    pid = mkproject("live")
    seed = mkdoc(
        pid,
        [
            ("Open the lid.", "Abra la tapa.", 100, None),
            ("Close the lid now.", "Cierre la tapa ahora.", 100, None),
            ("Pack the bag today.", "Prepare la bolsa hoy.", 100, None),
        ],
    )
    with Session(engine) as s:
        segs = s.exec(
            select(Segment).where(Segment.document_id == seed).order_by(Segment.order_index)
        ).all()
        # first two with their real neighbours (context), the third without any (plain 100)
        tm.write_back(s, pid, segs[0].source_text, "Abra la tapa.", SRC, DE, segment=segs[0])
        tm.write_back(
            s, pid, segs[1].source_text, "Cierre la tapa ahora.", SRC, DE, segment=segs[1]
        )
        tm.write_back(s, pid, segs[2].source_text, "Prepare la bolsa hoy.", SRC, DE)
    doc_b = mkdoc(
        pid,
        [
            ("Open the lid.", "", None, None),
            ("Close the lid now.", "", None, None),
            ("Pack the bag today.", "", None, None),
            ("A sentence nobody translated.", "", None, None),
        ],
    )
    r = run_analyze(pid, document_ids=[doc_b])
    # first two share the seed document's neighbours (101); the third has no stored context
    # (100); the last is unknown (new).
    assert bands(r, DE) == {
        "context": (2, 7),
        "repetitions": (0, 0),
        "exact": (1, 4),
        "new": (1, 4),
    }, bands(r, DE)
    # same source with other neighbours: a 100, not a 101
    doc_c = mkdoc(
        pid,
        [
            ("Something else entirely first.", "", None, None),
            ("Close the lid now.", "", None, None),
            ("And one more thing last.", "", None, None),
        ],
    )
    r = run_analyze(pid, document_ids=[doc_c])
    assert bands(r, DE)["exact"] == (1, 4) and bands(r, DE)["context"] == (0, 0), bands(r, DE)
    # a filled segment is NOT looked up: the same unfilled doc, filled as MT, is all new
    doc_d = mkdoc(
        pid, [("Open the lid.", "Abra.", None, None), ("Close the lid now.", "x", None, None)]
    )
    r = run_analyze(pid, document_ids=[doc_d])
    assert bands(r, DE)["new"] == (2, 7) and bands(r, DE)["context"] == (0, 0), bands(r, DE)
    print(
        " 2 live lookup: 101 with the same neighbours, 100 otherwise, filled rows keep their band"
    )


def test_key_project_101_from_string_key():
    pid = mkproject("keys")
    with Session(engine) as s:
        sk = StringKey(project_id=pid, namespace="ui", name="save")
        sk2 = StringKey(project_id=pid, namespace="ui", name="store")
        dead = StringKey(project_id=pid, namespace="ui", name="gone")
        s.add(sk)
        s.add(sk2)
        s.add(dead)
        s.commit()
        for k in (sk, sk2, dead):
            s.refresh(k)
        from app.models import _utcnow

        dead.deleted_at = _utcnow()
        s.add(dead)
        kdoc = Document(project_id=pid, filename="keys", fmt="keys")
        s.add(kdoc)
        s.commit()
        s.refresh(kdoc)
        segs = []
        for i, (k, src) in enumerate([(sk, "Save"), (sk2, "Store the file"), (dead, "Gone away")]):
            seg = Segment(
                document_id=kdoc.id,
                target_locale=DE,
                order_index=i,
                source_text=src,
                target_text="",
                key_id=k.id,
            )
            s.add(seg)
            segs.append(seg)
        s.commit()
        for seg in segs:
            s.refresh(seg)
        tm.write_back(s, pid, "Save", "Speichern", SRC, DE, segment=segs[0])
    r = run_analyze(pid)
    assert bands(r, DE) == {
        "context": (1, 1),
        "repetitions": (0, 0),
        "exact": (0, 0),
        "new": (1, 3),
    }, bands(r, DE)  # the soft-deleted key is not counted
    # a plain source (no key) with the same text is only a 100 against that TM row
    d = mkdoc(pid, [("Save", "", None, None)])
    r = run_analyze(pid, document_ids=[d])
    assert bands(r, DE)["exact"] == (1, 1) and bands(r, DE)["context"] == (0, 0)
    print(" 3 key project: 101 from the string key, key content in the project scope")


def test_languages_do_not_share_repetitions():
    pid = mkproject("langs", targets=f"{DE},{FR}")
    mkdoc(pid, [("Shared source text.", "", None, None)], locale=DE)
    d = mkdoc(pid, [("Shared source text.", "", None, None)], locale=DE)
    with Session(engine) as s:
        doc = s.get(Document, d)
        s.add(
            Segment(
                document_id=doc.id,
                target_locale=FR,
                order_index=0,
                source_text="Shared source text.",
                target_text="",
            )
        )
        s.commit()
    r = run_analyze(pid)
    de, fr = bands(r, DE), bands(r, FR)
    assert de["repetitions"] == (1, 3) and de["new"] == (1, 3), de
    assert fr["repetitions"] == (0, 0) and fr["new"] == (1, 3), fr
    assert [x["locale"] for x in r["languages"]] == [DE, FR]
    assert r["totals"]["words"] == 9 and r["totals"]["segments"] == 3
    print(" 4 languages: the same source in de and fr is not a repetition")


def make_job(doc_id, locale, kind="translate_document", created_by="admin@x", payload=None):
    with Session(engine) as s:
        j = Job(
            kind=kind,
            status=JobStatus.COMPLETED,
            payload=payload if payload is not None else {"document_id": doc_id, "locale": locale},
            result={},
            created_by=created_by,
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def test_job_scope_vs_project_scope():
    pid = mkproject("jobs", targets=f"{DE},{FR}")
    d1 = mkdoc(pid, [("One two three", "x", 100, None), ("Four five", "y", None, None)], DE)
    mkdoc(pid, [("One two three", "x", 100, None), ("Six", "", None, None)], DE)
    with Session(engine) as s:
        s.add(
            Segment(
                document_id=d1,
                target_locale=FR,
                order_index=0,
                source_text="Fr only",
                target_text="a",
            )
        )
        s.commit()
    proj = run_analyze(pid)
    jid = make_job(d1, DE)
    with Session(engine) as s:
        job = s.get(Job, jid)
        rep = leverage.analyze(s, None, job_id=job.id)
        same = leverage.analyze(s, pid, job_id=job.id)
    assert rep == same
    assert rep["scope"] == "job" and rep["job_id"] == jid and rep["project_id"] == pid
    assert [x["locale"] for x in rep["languages"]] == [DE]
    assert bands(rep, DE) == {
        "context": (0, 0),
        "repetitions": (0, 0),
        "exact": (1, 3),
        "new": (1, 2),
    }, bands(rep, DE)
    # project scope is wider: both documents and both languages, and d2's copy is a repetition
    assert [x["locale"] for x in proj["languages"]] == [DE, FR]
    assert proj["totals"]["segments"] == 5 and bands(proj, DE)["repetitions"] == (1, 3)
    # a job kind with no documents is a clear 400
    for kind, payload in (
        ("connector_translate", {"connection_id": 1}),
        ("backfill_embeddings", {}),
    ):
        j2 = make_job(None, None, kind=kind, payload=payload)
        with Session(engine) as s:
            try:
                leverage.analyze(s, None, job_id=j2)
            except leverage.LeverageError as exc:
                assert exc.status == 400 and "no documents" in str(exc), exc
            else:
                raise AssertionError("a job without documents must be refused")
    with Session(engine) as s:
        try:
            leverage.analyze(s, None, job_id=999999)
        except leverage.LeverageError as exc:
            assert exc.status == 404
        else:
            raise AssertionError("missing job")
    print(" 5 job scope: one document and language; no-document jobs are a 400")
    return pid, jid


def test_csv_shape(pid):
    r = run_analyze(pid)
    text = leverage.to_csv(r)
    assert "\r" not in text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["scope", "language", "band", "segments", "words"], rows[0]
    n_lang = len(r["languages"])
    assert len(rows) == 1 + (n_lang + 1) * 5, len(rows)
    first = rows[1:6]
    assert [x[2] for x in first] == ["context", "repetitions", "exact", "new", "total"]
    assert first[0][0] == f"project {pid}" and first[0][1] == r["languages"][0]["locale"]
    assert int(first[4][3]) == r["languages"][0]["segments"]
    assert int(first[4][4]) == r["languages"][0]["words"]
    assert rows[-1][1] == "all" and rows[-1][2] == "total"
    assert int(rows[-1][4]) == r["totals"]["words"]
    # L2: a locale value that starts with "=" is neutralized (formula injection)
    evil = {
        "languages": [
            {
                "locale": "=HYPERLINK(1)",
                "bands": r["languages"][0]["bands"],
                "segments": 0,
                "words": 0,
            }
        ],
        "totals": r["totals"],
        "project_id": pid,
        "job_id": None,
    }
    erows = list(csv.reader(io.StringIO(leverage.to_csv(evil))))
    assert erows[1][1] == "'=HYPERLINK(1)", erows[1]
    assert not any(c.startswith("=") for row in erows for c in row)
    print(" 6 csv: header, 4 bands + total per language, an all block")


def cli(*argv):
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(list(argv))
    return rc, buf.getvalue()


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_surface_parity(pid, jid):
    import uvicorn

    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import server as mcp_server

    admin = security.create_user("lev-admin@x", "Lev", Role.ADMIN, FIXTURE_PW)
    _row, key = apikeys.create_key(admin.id, "lev", scopes="api docs")
    want_p = run_analyze(pid)
    want_j = run_analyze(pid, job_id=jid)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        mcp_server.BASE_URL = base
        mcp_server.TOKEN = key
        client = TestClient(app)
        h = {"X-Loc-Token": key}

        # --- API ---
        r = client.get(f"/api/projects/{pid}/leverage", headers=h)
        assert r.status_code == 200, r.text
        api_p = r.json()
        api_p.pop("ok")
        assert api_p == want_p
        r = client.get(f"/api/jobs/{jid}/leverage", headers=h)
        assert r.status_code == 200, r.text
        api_j = r.json()
        api_j.pop("ok")
        assert api_j == want_j
        # CSV
        r = client.get(f"/api/projects/{pid}/leverage?format=csv", headers=h)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert r.text == leverage.to_csv(want_p)
        assert "attachment" in r.headers["content-disposition"]
        r = client.get(f"/api/jobs/{jid}/leverage?format=csv", headers=h)
        assert r.text == leverage.to_csv(want_j)
        assert client.get(f"/api/projects/{pid}/leverage?format=xml", headers=h).status_code == 400
        # a job with no documents is a 400, not an empty report
        bad = make_job(None, None, kind="backfill_embeddings", payload={})
        r = client.get(f"/api/jobs/{bad}/leverage", headers=h)
        assert r.status_code == 400 and "no documents" in r.json()["error"], r.text
        # the project page renders the table and the CSV link
        page = client.get(f"/projects/{pid}")
        assert page.status_code == 200
        assert "data-leverage-table" in page.text
        assert f"/projects/{pid}/leverage.csv" in page.text
        assert (
            "101 % in-context" in page.text
            and "fuzzy"
            not in page.text.lower().split("word-based leverage")[-1].split("</section>")[0]
        )

        r = client.get(f"/projects/{pid}/leverage.csv")
        assert r.status_code == 200 and r.text == leverage.to_csv(want_p)
        assert client.get("/projects/987654/leverage.csv").status_code in (404, 403)
        r = client.get(f"/jobs/{jid}/leverage")
        assert r.status_code == 200 and "data-leverage-table" in r.text, r.text[:300]
        assert f"/jobs/{jid}/leverage.csv" in r.text
        r = client.get(f"/jobs/{jid}/leverage.csv")
        assert r.status_code == 200 and r.text == leverage.to_csv(want_j)
        assert client.get(f"/jobs/{bad}/leverage").status_code == 400
        assert client.get("/jobs/987654/leverage").status_code == 404

        # --- CLI local ---
        rc, out = cli("--json", "project", "leverage", str(pid))
        assert rc == 0
        got = json.loads(out.strip().splitlines()[-1])
        got.pop("ok")
        assert got == want_p
        rc, out = cli("--json", "job", "leverage", str(jid))
        assert rc == 0
        got = json.loads(out.strip().splitlines()[-1])
        got.pop("ok")
        assert got == want_j
        rc, out = cli("project", "leverage", str(pid), "--csv")
        assert rc == 0 and out == leverage.to_csv(want_p)
        rc, out = cli("project", "leverage", str(pid))
        assert rc == 0 and "101 % in-context" in out and "all" in out
        out_file = os.path.join(tempfile.mkdtemp(prefix="loctms_lev_"), "lev.csv")
        rc, _ = cli("job", "leverage", str(jid), "-o", out_file)
        assert rc == 0
        with open(out_file, encoding="utf-8", newline="") as fh:
            assert fh.read() == leverage.to_csv(want_j)
        rc, _ = cli("job", "leverage", str(jid), "-o", out_file)  # exists, no --force
        assert rc == 2
        rc, _ = cli("job", "leverage", str(jid), "-o", out_file, "--force")
        assert rc == 0
        rc, _ = cli("job", "leverage", str(bad))
        assert rc == 2

        # --- CLI remote ---
        rc, out = cli("--remote", base, "--token", key, "--json", "project", "leverage", str(pid))
        assert rc == 0
        got = json.loads(out.strip().splitlines()[-1])
        got.pop("ok")
        assert got == want_p
        rc, out = cli("--remote", base, "--token", key, "--json", "job", "leverage", str(jid))
        assert rc == 0
        got = json.loads(out.strip().splitlines()[-1])
        got.pop("ok")
        assert got == want_j
        rc, out = cli("--remote", base, "--token", key, "project", "leverage", str(pid), "--csv")
        assert rc == 0 and out == leverage.to_csv(want_p), out
        rc, out = cli("--remote", base, "--token", key, "job", "leverage", str(jid), "--csv")
        assert rc == 0 and out == leverage.to_csv(want_j)

        # --- MCP ---
        def mcp(tool, **args):
            return json.loads(asyncio.run(mcp_server.call_tool_unvalidated(tool, args)))

        m = mcp("list_projects", view="leverage", project_id=pid)
        assert m["project_id"] == pid and m["leverage"] == {
            k: v for k, v in want_p.items() if k != "project_id"
        }, m
        m = mcp("job_status", view="leverage", job_id=jid)
        assert m["job_id"] == jid and m["leverage"] == want_j, m
        try:
            asyncio.run(mcp_server.call_tool_unvalidated("job_status", {"view": "leverage"}))
        except Exception as exc:
            assert "needs a job_id" in str(exc), exc
        else:
            raise AssertionError("view=leverage without job_id must be refused")
    finally:
        server.should_exit = True
        th.join(timeout=10)
    print(" 6b parity: API, CLI (local + remote), MCP and the project page agree; CSV identical")


def test_rate_card():
    # save: fuzzy keys are refused with the ADR message
    for key in vendor_cost.LEGACY_FUZZY_BANDS:
        try:
            vendor_cost.validate_rate_card({"bands": {"new": 0.1, key: 0.05}})
        except vendor_cost.RateCardError as exc:
            assert "fuzzy rate bands were removed" in str(exc) and "ADR 0102" in str(exc), exc
            assert "101 % / 100 % / repetitions / new" in str(exc)
        else:
            raise AssertionError(f"{key} must be refused")
    try:
        vendor_cost.validate_rate_card({"bands": {"bogus": 1}})
    except vendor_cost.RateCardError as exc:
        assert "unknown rate band" in str(exc)
    vendor_cost.validate_rate_card({"currency": "EUR", "bands": {"new": 0.1, "context": 0}})
    with Session(engine) as s:
        v = Vendor(name=f"Lev vendor {_seq()}")
        s.add(v)
        s.commit()
        s.refresh(v)
        # a card stored before ADR 0102 loads with the fuzzy keys dropped, never crashes
        v.rate_card = {
            "currency": "usd",
            "bands": {
                "new": 0.2,
                "exact": 0.05,
                "fuzzy_high": 0.15,
                "fuzzy_mid": 0.16,
                "fuzzy_low": 0.17,
            },
        }
        s.add(v)
        s.commit()
        s.refresh(v)
        card = vendors.rate_card_for(v)
        assert list(card["bands"]) == list(vendor_cost.BANDS), card
        assert card["bands"]["new"] == 0.2 and card["bands"]["exact"] == 0.05
        assert not any(k.startswith("fuzzy") for k in card["bands"])
        try:
            vendors.set_rate_card(s, v.id, {"bands": {"fuzzy_mid": 0.1}})
        except vendors.VendorError as exc:
            assert "fuzzy rate bands were removed" in str(exc)
        else:
            raise AssertionError("set_rate_card must refuse fuzzy keys")
        # a rows-with-old-stamp segment prices as new
        est = vendor_cost.estimate_from_segments(
            [
                {"source_text": "a b c", "tm_match_pct": 88},
                {"source_text": "d e", "tm_match_pct": 100},
            ],
            v.rate_card,
        )
        assert est["bands"]["new"]["words"] == 3 and est["bands"]["exact"]["words"] == 2
        assert list(est["bands"]) == list(vendor_cost.BANDS)
        assert abs(est["total_cost"] - (3 * 0.2 + 2 * 0.05)) < 1e-9, est
    # a persisted estimate from before folds its fuzzy cells into new
    rows = vendor_cost.estimate_band_rows(
        {
            "bands": {
                "new": {"segments": 1, "words": 2, "cost": 0.4},
                "fuzzy_high": {"segments": 1, "words": 5, "cost": 1.0},
                "exact": {"segments": 1, "words": 3, "cost": 0},
            }
        }
    )
    assert [r["band"] for r in rows] == list(vendor_cost.BANDS)
    new = next(r for r in rows if r["band"] == "new")
    assert new["words"] == 7 and new["segments"] == 2 and abs(new["cost"] - 1.4) < 1e-9, new
    # the vendor dispatch contract no longer produces tm_fuzzy / match_score
    assert not any("fuzzy" in v for v in vendor_cost.PROVENANCE_BY_BAND.values())
    assert not any("fuzzy" in v for v in vendor_cost.XLIFF_STATE_QUALIFIER.values())
    print(" 7 rate card: fuzzy keys refused on save, dropped on load, old estimates fold into new")


def test_provenance_bar_bands():
    assert tm.LEVERAGE_BANDS == ("context", "exact", "new", "untranslated")
    segs = [
        {"target_text": "t", "tm_match_pct": 101},
        {"target_text": "t", "tm_match_pct": 100},
        {"target_text": "t", "tm_match_pct": 88},  # old fuzzy stamp -> new
        {"target_text": "t", "tm_match_pct": None},
        {"target_text": "", "tm_match_pct": 100},
    ]
    assert tm_common.leverage_bands(segs) == {"context": 1, "exact": 1, "new": 2, "untranslated": 1}
    print(" 8 provenance bar: context / exact / new / untranslated; old 75-99 is new")


def test_confinement():
    with Session(engine) as s:
        oa, ob = Organization(name=f"LevOrgA-{_seq()}"), Organization(name=f"LevOrgB-{_seq()}")
        s.add(oa)
        s.add(ob)
        s.commit()
        org_a, org_b = oa.id, ob.id
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        pa = mkproject("orgA", org_id=org_a)
        pb = mkproject("orgB", org_id=org_b)
        da = mkdoc(pa, [("Org A text here", "", None, None)])
        db = mkdoc(pb, [("Org B text here", "", None, None)])
        ua = security.create_user("lev-a@x", "A", Role.ADMIN, FIXTURE_PW, org_id=org_a)
        ub = security.create_user("lev-b@x", "B", Role.ADMIN, FIXTURE_PW, org_id=org_b)
        with Session(engine) as s:
            for u, o in ((ua, org_a), (ub, org_b)):
                row = s.get(User, u.id)
                row.org_id = o
                s.add(row)
            s.commit()
        ja = make_job(da, DE, created_by="lev-a@x")
        jb = make_job(db, DE, created_by="lev-b@x")
        _r, ka = apikeys.create_key(ua.id, "lev a", scopes="api docs")
        _r, kb = apikeys.create_key(ub.id, "lev b", scopes="api docs")
        c = TestClient(app)
        ha, hb = {"X-Loc-Token": ka}, {"X-Loc-Token": kb}
        assert c.get(f"/api/projects/{pa}/leverage", headers=ha).status_code == 200
        assert c.get(f"/api/jobs/{ja}/leverage", headers=ha).status_code == 200
        foreign_p = c.get(f"/api/projects/{pa}/leverage", headers=hb)
        missing_p = c.get("/api/projects/987654/leverage", headers=hb)
        assert foreign_p.status_code == 404 and foreign_p.json() == missing_p.json()
        foreign_j = c.get(f"/api/jobs/{ja}/leverage", headers=hb)
        missing_j = c.get("/api/jobs/987654/leverage", headers=hb)
        assert foreign_j.status_code == 404 and foreign_j.json() == missing_j.json()
        assert c.get(f"/api/jobs/{jb}/leverage?format=csv", headers=ha).status_code == 404
        assert c.get(f"/api/projects/{pb}/leverage?format=csv", headers=ha).status_code == 404

        # the WEB routes (session cookie): a foreign org's project/job is the same 404 as a
        # missing one
        def web(email):
            from tests._csrf import csrf_for

            wc = TestClient(app, follow_redirects=False)
            csrf_for(wc)
            r = wc.post("/auth/login", data={"email": email, "password": FIXTURE_PW, "next": "/"})
            assert r.status_code == 303, ("login", email, r.status_code)
            return wc

        wa, wb = web("lev-a@x"), web("lev-b@x")
        assert wa.get(f"/projects/{pa}/leverage.csv").status_code == 200
        assert wa.get(f"/jobs/{ja}/leverage").status_code == 200
        assert wa.get(f"/jobs/{ja}/leverage.csv").status_code == 200
        miss_p = wb.get("/projects/987654/leverage.csv")
        miss_j = wb.get("/jobs/987654/leverage")
        assert miss_p.status_code == 404 and miss_j.status_code == 404, (
            miss_p.status_code,
            miss_j.status_code,
            miss_p.text[:200],
        )
        for wc, url, miss in (
            (wb, f"/projects/{pa}/leverage.csv", miss_p),
            (wb, f"/jobs/{ja}/leverage", miss_j),
            (wb, f"/jobs/{ja}/leverage.csv", miss_j),
        ):
            got = wc.get(url)
            assert got.status_code == miss.status_code == 404, (url, got.status_code)
        # a customer-bound user of the SAME org sees only their own customer's project
        pa2 = mkproject("orgA2", org_id=org_a)
        uc = security.create_user("lev-c@x", "C", Role.ADMIN, FIXTURE_PW, org_id=org_a)
        with Session(engine) as s:
            row = s.get(User, uc.id)
            row.customer_id = s.get(Project, pa2).customer_id
            s.add(row)
            s.commit()
        wcu = web("lev-c@x")
        assert wcu.get(f"/projects/{pa2}/leverage.csv").status_code == 200
        miss_c = wcu.get("/projects/987654/leverage.csv")
        assert miss_c.status_code == 404
        assert wcu.get(f"/projects/{pa}/leverage.csv").status_code == 404
        assert wcu.get(f"/jobs/{ja}/leverage").status_code == 404
        assert wcu.get(f"/jobs/{ja}/leverage.csv").status_code == 404
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(" 9 confinement: another org's project or job is the same 404 as a missing one")


def test_speed_5000_segments():
    pid = mkproject("speed", targets=f"{DE},{FR}")
    import random

    rng = random.Random(7)
    words = [f"w{i}" for i in range(400)]
    with Session(engine) as s:
        for loc in (DE,):
            for dn in range(10):
                d = Document(project_id=pid, filename=f"big{dn}.txt", fmt="txt")
                s.add(d)
                s.commit()
                s.refresh(d)
                rows = []
                for i in range(500):
                    src = " ".join(rng.choice(words) for _ in range(rng.randint(4, 9)))
                    filled = i % 3 == 0
                    rows.append(
                        Segment(
                            document_id=d.id,
                            target_locale=loc,
                            order_index=i,
                            source_text=src,
                            target_text="t" if filled else "",
                            tm_match_pct=100 if i % 6 == 0 else None,
                        )
                    )
                s.add_all(rows)
                s.commit()
    t0 = time.perf_counter()
    r = run_analyze(pid)
    dt = time.perf_counter() - t0
    assert r["totals"]["segments"] == 5000, r["totals"]
    print(f" 10 speed: 5,000 segments, 1 language, 10 documents: {dt:.2f} s")
    assert dt < 20, dt
    return dt


def main():
    test_vocabulary_and_precedence_on_filled_segments()
    test_live_lookup_with_context()
    test_key_project_101_from_string_key()
    test_languages_do_not_share_repetitions()
    pid, jid = test_job_scope_vs_project_scope()
    test_csv_shape(pid)
    test_rate_card()
    test_provenance_bar_bands()
    test_surface_parity(pid, jid)
    test_confinement()
    test_speed_5000_segments()
    print("verify_leverage_report OK")


if __name__ == "__main__":
    main()
