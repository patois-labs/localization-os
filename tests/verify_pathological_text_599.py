"""Pathological text never freezes the app (#599).

On 2026-10-07 one POST /api/translate/text with "e" + 5000 combining acute accents froze
production for about 3.5 minutes: no request at all (not even /health) was answered. The
route was `async def` and ran the whole engine call (network stream, retry backoff sleeps,
the truncation-escalation ladder that a mark flood walks to the model's ceiling) INLINE on
the event loop. Every text-processing step in this app is linear on such input (profiled on
every surface below), so the fix is structural, not a faster regex:

* the blocking half of /api/translate/text, and of every other async route that ran an
  engine, a connector run or a file parse inline, now runs on the bounded worker pool
  (deps.offload_blocking); an AST ratchet keeps any such call off the loop;
* /api/translate/text (and its MCP tool, which proxies it) refuses a user-perceived character
  (extended grapheme cluster) over TRANSLATE_TEXT_MAX_CLUSTER_CODEPOINTS code points with a
  plain 422, before any engine call; real text is never close to the limit.

Checks:
  1. the cluster check accepts real text in many scripts and refuses floods, in linear time;
  2. /api/translate/text refuses every flood variant fast with a 422 and never calls an
     engine; real and merely long inputs still translate;
  3. every other surface (txt/xliff/docx upload, the MT job, approve, editor save, LQA run,
     TM import, glossary import) finishes each pathological input within a generous budget;
  4. LIVENESS: with a slow engine, /health answers while /api/translate/text is in flight
     (on the old code /health waited for the whole engine call);
  5. MCP translate_text surfaces the same 422 message;
  6. ratchet: no async handler in app/web calls an engine, connector run or parser inline.
"""

import ast
import base64
import io
import os
import re
import socket
import sys
import threading
import time
import zipfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_patho599_")

import httpx
import uvicorn
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import Segment, User
from app.services import apikeys, content, providers_hub
from app.services.providers import Provider, register_provider

init_db()

N = 5000  # the production payload size
BIG = 50000  # the "50k-char string" variants
# Generous: every surface measured at <= 1.4 s on these inputs; the budget only has to catch a
# super-linear path (the incident took 212 s), never a slow box.
SURFACE_BUDGET_S = 15.0
REFUSAL_BUDGET_S = 5.0

FLOODS = {
    "combining acute x5000 (the incident)": "e" + "́" * N,
    "mixed combining marks x5000": "a" + "".join(chr(0x300 + (i % 0x70)) for i in range(N)),
    "emoji ZWJ chain x5000": "\U0001f468" + "‍\U0001f469" * (N // 2),
    "emoji skin-tone modifiers x5000": "\U0001f44d" + "\U0001f3fb" * N,
    "zalgo words (50 marks each)": " ".join("x" + "́" * 50 for _ in range(100)),
    "flood after normal text": "Hello world. " * 20 + "o" + "̈" * 400,
}
LONG_OK = {
    "RTL marks x50k": "‏‮" * (BIG // 2) + "abc",
    "one 50k-char word": "a" * BIG,
    "50k zero-width spaces": "a" + "​" * BIG,
}
REAL_TEXT = [
    "Tiếng Việt có dấu",  # Vietnamese, precomposed
    "Tiếng Việt",  # Vietnamese, decomposed (2 marks stacked)
    "हिन्दी क्षत्रिय",
    "กำลังใจ น้ำ๊",  # Thai
    "བསྒྲུབས",  # Tibetan stack
    "בְּרֵאשִׁ֖ית",  # Hebrew
    "بِسْمِ ٱللَّهِ",
    "\U0001f468\U0001f3fb‍\U0001f469\U0001f3fb‍\U0001f467\U0001f3fb‍"
    "\U0001f466\U0001f3fb family",  # longest common ZWJ family, all skin tones (15)
    "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f flag",
    "العربية ‏(RTL)‎ mixed",
    "Z͑ͫ̓ͪ̂ͫ̽͏̴̙̤̞͉ mild",
]


class _CountingEngine(Provider):
    """A configured engine that counts calls and answers like the mock."""

    name = "counting"

    def __init__(self, delay: float = 0.0):
        self.calls = 0
        self.delay = delay

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)  # a slow engine stream, like a long real completion
        payload = payload or {}
        return f"[{payload.get('target', 'xx')}] {payload.get('text', '')}"


def _engine_names() -> list[str]:
    return sorted({"mock", (config.DEFAULT_PROVIDER or "mock").lower()})


def _install(eng) -> dict:
    saved = {n: providers_hub._OVERRIDES.get(n) for n in _engine_names()}
    for n in _engine_names():
        register_provider(n, eng)
    return saved


def _restore(saved: dict) -> None:
    for n, prev in saved.items():
        if prev is None:
            providers_hub._OVERRIDES.pop(n, None)
        else:
            providers_hub._OVERRIDES[n] = prev


def _token() -> str:
    with Session(engine) as s:
        user = s.exec(select(User)).first()
        if user is None:
            user = User(email="patho599@example.test", role="admin", org_id=1, active=True)
            s.add(user)
            s.commit()
            s.refresh(user)
        uid = user.id
    return apikeys.create_key(uid, name="patho599", scopes="api docs")[1]


TOK = _token()
API = TestClient(app)
API.headers["X-Loc-Token"] = TOK
WEB = TestClient(app)


def _timed(label, fn, budget=SURFACE_BUDGET_S):
    t = time.perf_counter()
    r = fn()
    dt = time.perf_counter() - t
    assert dt < budget, f"{label}: took {dt:.1f} s (budget {budget} s)"
    return r, dt


# ---------------------------------------------------------------------------------------------
def test_cluster_check_unit():
    from app.services.translate import stacked_cluster_error

    for text in REAL_TEXT:
        assert stacked_cluster_error(text) == "", (
            "real text refused",
            text.encode("unicode_escape"),
        )
    for label, text in FLOODS.items():
        msg = stacked_cluster_error(text)
        assert "stacked marks" in msg and "limit 32" in msg, (label, msg)
    for label, text in LONG_OK.items():
        assert stacked_cluster_error(text) == "", label
    # exactly at the limit passes, one over refuses
    limit = config.TRANSLATE_TEXT_MAX_CLUSTER_CODEPOINTS
    assert stacked_cluster_error("e" + "́" * (limit - 1)) == ""
    assert stacked_cluster_error("e" + "́" * limit) != ""
    # linear: the largest accepted body in one pass, well under a second
    for text in ("e" + "́" * 200_000, "a" * 200_000, ("x" + "́" * 31 + " ") * 6000):
        t = time.perf_counter()
        stacked_cluster_error(text)
        assert time.perf_counter() - t < 1.0, "cluster check is not linear"
    print("  cluster check: real text in 11 scripts passes, floods refused, linear OK")


def test_translate_text_refuses_floods():
    eng = _CountingEngine()
    saved = _install(eng)
    try:
        for label, text in FLOODS.items():
            r, _dt = _timed(
                label,
                lambda text=text: API.post(
                    "/api/translate/text", json={"text": text, "target_locale": "fr"}
                ),
                REFUSAL_BUDGET_S,
            )
            assert r.status_code == 422, (label, r.status_code, r.text[:200])
            body = r.json()
            assert body["ok"] is False and "stacked marks" in body["error"], (label, body)
        assert eng.calls == 0, f"a refused flood still reached the engine ({eng.calls} calls)"
        for text in REAL_TEXT + list(LONG_OK.values()):
            r, _ = _timed(
                "real text",
                lambda text=text: API.post(
                    "/api/translate/text", json={"text": text, "target_locale": "fr"}
                ),
            )
            assert r.status_code == 200 and r.json()["ok"] is True, (r.status_code, r.text[:200])
        assert eng.calls == len(REAL_TEXT) + len(LONG_OK), eng.calls
    finally:
        _restore(saved)
    print("  /api/translate/text: 6 flood shapes -> 422 before any engine call; real text OK")


def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"><Default Extension="rels" ContentType="application/vnd.openxml'
            'formats-package.relationships+xml"/><Default Extension="xml" ContentType='
            '"application/xml"/><Override PartName="/word/document.xml" ContentType="'
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
            '"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
            "openxmlformats.org/officeDocument/2006/relationships/officeDocument"
            '" Target="word/document.xml"/></Relationships>',
        )
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>'
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
            f"<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Bold {text}</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )
    return buf.getvalue()


def _xliff(text: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><xliff version="1.2" xmlns="urn:oasis:names:tc:'
        'xliff:document:1.2"><file source-language="en-US" target-language="fr-FR" '
        'datatype="plaintext" original="x"><body>'
        f'<trans-unit id="1"><source>{text}</source><target>{text}</target></trans-unit>'
        "</body></file></xliff>"
    ).encode()


def _tmx(text: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="en-US" '
        'datatype="plaintext" segtype="sentence" adminlang="en" creationtool="x" '
        'creationtoolversion="1" o-tmf="x"/><body>'
        f'<tu><tuv xml:lang="en-US"><seg>{text}</seg></tuv>'
        f'<tuv xml:lang="fr-FR"><seg>{text}</seg></tuv></tu></body></tmx>'
    ).encode()


def test_every_surface_within_budget():
    variants = {
        "combining x5000": FLOODS["combining acute x5000 (the incident)"],
        "combining x50k": "e" + "́" * BIG,
        "ZWJ chain x50k": FLOODS["emoji ZWJ chain x5000"] * 10,
        "modifiers x50k": "\U0001f44d" + "\U0001f3fb" * BIG,
        **LONG_OK,
    }
    with Session(engine) as s:
        pid = content.project_create(
            s, "Patho 599", source_locale="en-US", target_locales=["fr-FR"]
        ).id
    worst = ("", 0.0)
    for label, text in variants.items():
        steps = []
        r, dt = _timed(
            f"{label}: upload txt",
            lambda text=text: API.post(
                "/api/documents",
                data={"project_id": str(pid)},
                files={"file": ("p.txt", (text + "\nHello world.\n").encode(), "text/plain")},
            ),
        )
        assert r.status_code == 200, (label, r.status_code, r.text[:200])
        doc_id = r.json()["document_id"]
        steps.append(dt)
        for name, fname, data in (
            ("xliff", "p.xlf", _xliff(text)),
            ("docx", "p.docx", _docx(text)),
        ):
            r, dt = _timed(
                f"{label}: upload {name}",
                lambda fname=fname, data=data: API.post(
                    "/api/documents",
                    data={"project_id": str(pid)},
                    files={"file": (fname, data, "application/octet-stream")},
                ),
            )
            assert r.status_code == 200, (label, name, r.status_code, r.text[:200])
            steps.append(dt)
        t = time.perf_counter()
        r = API.post(
            f"/api/documents/{doc_id}/translate", json={"targets": ["fr-FR"], "provider": "mock"}
        )
        assert r.status_code == 202, (label, r.status_code, r.text[:200])
        jid = r.json()["jobs"][0]["job_id"]
        while True:
            j = API.get(f"/api/jobs/{jid}").json()
            if j.get("status") in ("completed", "failed", "cancelled"):
                break
            assert time.perf_counter() - t < SURFACE_BUDGET_S, f"{label}: MT job still {j}"
            time.sleep(0.1)
        assert j.get("status") == "completed" and not j.get("error"), (label, j)
        steps.append(time.perf_counter() - t)
        with Session(engine) as s:
            sid = (
                s.exec(select(Segment).where(Segment.document_id == doc_id).order_by(Segment.id))
                .first()
                .id
            )
        r, dt = _timed(
            f"{label}: approve", lambda sid=sid: API.post(f"/api/segments/{sid}/approve", json={})
        )
        assert r.status_code == 200, (label, r.status_code, r.text[:200])
        steps.append(dt)
        r, dt = _timed(
            f"{label}: editor save",
            lambda sid=sid, text=text: WEB.post(
                f"/segments/{sid}/save", data={"target_text": text + " x"}
            ),
        )
        assert r.status_code == 200, (label, r.status_code, r.text[:200])
        steps.append(dt)
        r, dt = _timed(
            f"{label}: LQA run",
            lambda doc_id=doc_id: WEB.post(
                f"/documents/{doc_id}/lqa/run", data={"locale": "fr-FR"}
            ),
        )
        assert r.status_code < 500, (label, r.status_code)
        steps.append(dt)
        r, dt = _timed(
            f"{label}: TM import",
            lambda text=text: API.post(
                f"/api/projects/{pid}/tm/import",
                json={
                    "tmx_b64": base64.b64encode(_tmx(text)).decode(),
                    "source": "en-US",
                    "target": "fr-FR",
                },
            ),
        )
        assert r.status_code == 200, (label, r.status_code, r.text[:200])
        steps.append(dt)
        csv = f"source_term,target_term\n{text},{text}\nHello,Bonjour\n".encode()
        r, dt = _timed(
            f"{label}: glossary import",
            lambda csv=csv: API.post(
                f"/api/projects/{pid}/glossary/import",
                json={
                    "csv_b64": base64.b64encode(csv).decode(),
                    "source": "en-US",
                    "target": "fr-FR",
                },
            ),
        )
        assert r.status_code == 200 and r.json()["ok"] is True, (label, r.status_code, r.text[:200])
        steps.append(dt)
        if max(steps) > worst[1]:
            worst = (label, max(steps))
    print(
        "  9 surfaces x %d inputs (upload txt/xliff/docx, MT job, approve, editor save, LQA, "  # noqa: UP031
        "TM import, glossary import) each under %d s; slowest %.2f s (%s) OK"
        % (len(variants), SURFACE_BUDGET_S, worst[1], worst[0])
    )


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    def __init__(self):
        self.port = _free_port()
        cfg = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", loop="asyncio"
        )
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 20
        while not self.server.started:
            assert time.time() < deadline, "uvicorn did not start"
            time.sleep(0.05)
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


ENGINE_DELAY_S = 6.0
HEALTH_BUDGET_S = 2.0


def test_health_answers_while_engine_busy():
    eng = _CountingEngine(delay=ENGINE_DELAY_S)
    saved = _install(eng)
    result = {}
    try:
        with _Server() as base:
            assert httpx.get(f"{base}/health", timeout=10).status_code == 200

            def slow_call():
                t = time.perf_counter()
                r = httpx.post(
                    f"{base}/api/translate/text",
                    headers={"X-Loc-Token": TOK},
                    json={"text": "Hello there", "target_locale": "fr"},
                    timeout=60,
                )
                result["status"], result["elapsed"] = r.status_code, time.perf_counter() - t

            worker = threading.Thread(target=slow_call)
            worker.start()
            deadline = time.time() + 10
            while eng.calls == 0:  # wait until the engine call is really in flight
                assert time.time() < deadline, "the translate call never reached the engine"
                time.sleep(0.02)
            t = time.perf_counter()
            try:
                h = httpx.get(f"{base}/health", timeout=ENGINE_DELAY_S + 10)
            finally:
                health_s = time.perf_counter() - t
            in_flight = worker.is_alive()
            worker.join(timeout=60)
    finally:
        _restore(saved)
    assert h.status_code == 200, h.status_code
    assert health_s < HEALTH_BUDGET_S, (
        f"/health took {health_s:.1f} s while one /api/translate/text engine call was in "
        f"flight: the engine call is blocking the event loop (#599)"
    )
    assert in_flight, "the translate call had already finished: the probe proves nothing"
    assert result.get("status") == 200 and result["elapsed"] >= ENGINE_DELAY_S - 0.5, result
    print(
        "  liveness: /health answered in %.2f s during a %.0f s engine call OK"  # noqa: UP031
        % (health_s, ENGINE_DELAY_S)
    )


def test_mcp_translate_text_refusal():
    mcp_dir = os.path.join(_ROOT, "integrations", "mcp-server")
    sys.path.insert(0, mcp_dir)
    import server as mcp_server

    with _Server() as base:
        mcp_server.BASE_URL, mcp_server.TOKEN = base, TOK
        try:
            from tests._mcp_plan import confirmed  # #757: plan first, then confirm

            confirmed(
                mcp_server,
                "translate_text",
                {
                    "text": FLOODS["combining acute x5000 (the incident)"],
                    "target_locale": "fr",
                    "confirm": True,
                },
            )
        except mcp_server.ToolError as exc:
            assert "stacked marks" in str(exc), str(exc)[:300]
        else:
            raise AssertionError("MCP translate_text accepted a combining-mark flood")
    print("  MCP translate_text: the flood is a tool error carrying the API's refusal message OK")


def test_postedit_distance_bounded():
    """The editor save's post-edit capture ran two full Levenshtein passes (O(n*m)), and
    rapidfuzz holds the GIL, so one save of a 100k-character target froze every thread."""
    from rapidfuzz.distance import Levenshtein

    from app.services import postedit

    big = config.MAX_SEGMENT_CHARS - 10
    for label, a, b in (
        ("marks", "[fr] e" + "́" * big, "e" + "́" * big + " x"),
        ("word", "[fr] " + "a" * big, "a" * big + " x"),
        ("rewrite", "x" * big, "y" * big),
    ):
        t = time.perf_counter()
        char, word, pct = postedit.distances(a, b)
        dt = time.perf_counter() - t
        assert dt < 1.0, f"post-edit distance on {label} x{big}: {dt:.2f} s"
        assert 0 <= pct <= 100 and char >= abs(len(a) - len(b)), (label, char, word, pct)
    # a small edit of a huge segment stays EXACT (the banded search is exact inside its band)
    a, b = "[fr] " + "a" * big, "a" * big + " x"
    assert postedit.distances(a, b)[0] == 7, postedit.distances(a, b)
    # everyday segments: byte-identical to the full computation
    for a, b in (("Hello world", "Hallo Welt"), ("", "x"), ("abc " * 500, "abd " * 500)):
        assert postedit.distances(a, b)[0] == Levenshtein.distance(a, b)
        assert postedit.distances(a, b)[2] == round(
            Levenshtein.normalized_distance(a, b) * 100.0, 4
        )
    print("  post-edit distance: 100k-char saves bounded under 1 s, small edits exact OK")


TM_BUDGET_S = 3.0


def test_tm_fuzzy_bounded():
    """TM fuzzy matching scored every candidate with a full fuzz.ratio (O(n*m), GIL held):
    a long source against many long TM rows held every thread for seconds per lookup."""
    import random

    from rapidfuzz import fuzz

    from app.services import tm, tm_hub

    rnd = random.Random(599)

    def junk(n):
        return "".join(rnd.choice("abcdefghij") for _ in range(n))

    with Session(engine) as s:
        pid = content.project_create(
            s, "Patho 599 TM", source_locale="en-US", target_locales=["fr-FR"]
        ).id
    rows = [junk(80_000) for _ in range(40)]
    rows += ["The quick brown fox jumps over the lazy dog.", "Save your changes first."]
    tus = "".join(
        f'<tu><tuv xml:lang="en-US"><seg>{r}</seg></tuv>'
        f'<tuv xml:lang="fr-FR"><seg>{r}</seg></tuv></tu>'
        for r in rows
    )
    tmx = (
        '<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="en-US" '
        'datatype="plaintext" segtype="sentence" adminlang="en" creationtool="x" '
        f'creationtoolversion="1" o-tmf="x"/><body>{tus}</body></tmx>'
    ).encode()
    r = API.post(
        f"/api/projects/{pid}/tm/import",
        json={"tmx_b64": base64.b64encode(tmx).decode(), "source": "en-US", "target": "fr-FR"},
    )
    assert r.status_code == 200 and r.json()["imported"] == len(rows), r.text[:300]
    query = junk(80_000)
    with Session(engine) as s:
        for label, fn in (
            ("lookup", lambda: tm.lookup(s, pid, query, "en-US", "fr-FR")),
            ("lookup_batch", lambda: tm_hub.lookup_batch(s, pid, [query], "en-US", "fr-FR")),
            (
                "example_matches_batch",
                lambda: tm_hub.example_matches_batch(s, pid, [query], "en-US", "fr-FR"),
            ),
        ):
            _timed(f"TM {label} of an 80k-char source over 40 80k-char rows", fn, TM_BUDGET_S)
        # ordinary fuzzy matching is unchanged
        near = "The quick brown fox jumped over the lazy dog."
        hit = tm.lookup(s, pid, near, "en-US", "fr-FR")
        assert hit and 75 <= hit["match_pct"] < 100, hit
        assert tm_hub.lookup_batch(s, pid, [near], "en-US", "fr-FR")[near] == hit
        assert tm_hub.example_matches_batch(s, pid, [near], "en-US", "fr-FR")[near], "no example"
    # the cutoff is exact: a score at or over the floor is the full score, below it is dropped
    for _ in range(3000):
        a = "".join(rnd.choice("abc ") for _ in range(rnd.randint(1, 60)))
        b = "".join(rnd.choice("abc ") for _ in range(rnd.randint(1, 60)))
        floor = rnd.choice([50, 75, 90, 100])
        full = fuzz.ratio(a, b)
        got = tm_hub._fuzzy_ratio(a, b, floor)
        assert (got == full) if full >= floor else (got < floor), (a, b, floor, full, got)
    print("  TM fuzzy: 80k-char lookups bounded (exact match only past the ceiling) OK")


SAVE_BUDGET_S = 5.0


def test_health_answers_during_huge_editor_save():
    big = config.MAX_SEGMENT_CHARS - 10
    text = "e" + "́" * big
    with Session(engine) as s:
        pid = content.project_create(
            s, "Patho 599 save", source_locale="en-US", target_locales=["fr-FR"]
        ).id
    r = API.post(
        "/api/documents",
        data={"project_id": str(pid)},
        files={"file": ("big.txt", (text + "\n").encode(), "text/plain")},
    )
    assert r.status_code == 200, (r.status_code, r.text[:200])
    doc_id = r.json()["document_id"]
    jid = API.post(
        f"/api/documents/{doc_id}/translate", json={"targets": ["fr-FR"], "provider": "mock"}
    ).json()["jobs"][0]["job_id"]
    t = time.perf_counter()
    while API.get(f"/api/jobs/{jid}").json().get("status") not in ("completed", "failed"):
        assert time.perf_counter() - t < 60, "MT job did not finish"
        time.sleep(0.1)
    with Session(engine) as s:
        sid = s.exec(select(Segment).where(Segment.document_id == doc_id)).first().id
    out = {}
    with _Server() as base:
        assert httpx.get(f"{base}/health", timeout=10).status_code == 200

        def save():
            t0 = time.perf_counter()
            r = httpx.post(
                f"{base}/segments/{sid}/save", data={"target_text": text + " x"}, timeout=120
            )
            out["status"], out["elapsed"] = r.status_code, time.perf_counter() - t0

        worker = threading.Thread(target=save)
        worker.start()
        time.sleep(0.3)  # the save is parsing and capturing by now
        t = time.perf_counter()
        h = httpx.get(f"{base}/health", timeout=120)
        health_s = time.perf_counter() - t
        worker.join(timeout=120)
    assert h.status_code == 200
    assert out.get("status") == 200, out
    assert out["elapsed"] < SAVE_BUDGET_S, f"editor save of a {big}-char target: {out}"
    assert health_s < HEALTH_BUDGET_S, f"/health took {health_s:.1f} s during an editor save"
    print(
        "  editor save of a %d-char target: %.2f s; /health during it %.2f s OK"  # noqa: UP031
        % (big, out["elapsed"], health_s)
    )


# The leaf names of calls that block for a long time: an engine call, a connector run (engine
# plus the external system), or an uploaded-file parse. An async handler must await them through
# deps.offload_blocking (or offload_parse); a plain call runs on the event loop and stalls every
# request, /health included (#599).
_BLOCKING = re.compile(
    r"^(translate_text_once|run_completion|translate_now|consume_airtable_payloads|"
    r"dynamic_translate|translate_fn|[a-z0-9]+_translate(_pending)?|import_pairs_csv|"
    r"import_keys|ingest_document|import_tmx|import_bytes|pretranslate_document)$"
)


def test_no_blocking_call_in_async_handlers():
    hits = []
    for f in sorted(Path("app/web").rglob("*.py")):
        tree = ast.parse(f.read_bytes())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            awaited = {id(c) for a in ast.walk(fn) if isinstance(a, ast.Await) for c in ast.walk(a)}
            for n in ast.walk(fn):
                if not isinstance(n, ast.Call) or id(n) in awaited:
                    continue
                leaf = (
                    n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                )
                if _BLOCKING.match(leaf):
                    hits.append(f"{f.as_posix()}:{n.lineno} {fn.name} -> {ast.unparse(n.func)}")
    assert not hits, "blocking work inline on the event loop:\n  " + "\n  ".join(hits)
    # the ratchet must see the real call sites it guards (a renamed seam would make it vacuous)
    src = Path("app/web/api_part2.py").read_text(encoding="utf-8")
    assert "deps.offload_blocking(\n            _translate_text_blocking," in src
    assert "translate.translate_text_once(" in src
    print("  ratchet: no engine call, connector run or file parse inline in an async handler OK")


# #611: connector routes whose store call reaches an external API (or takes the SQLite write
# lock, which can wait out the 5 s busy timeout). Inline in an async handler they stall /health
# and every other request for as long as the third party / the lock holder takes.
_CONN_NETWORK = {
    "jira_count",
    "salesforce_rediscover_fields",
    "notion_resolve_link",
    "helpscout_resolve_link",
    "wordpress_resolve_link",
    "register_airtable_webhook",
    "unregister_airtable_webhook",
    "airtable_webhook_status",
    "check_airtable_webhook_health",
    "airtable_create_records",
    "airtable_delete_records",
    "create",
    "update",
    "prune_orphaned_config",
    "patch_config",
    "rotate_secret",
    "set_oauth_secret",
    "schema_fields",
    "browse_containers_unsaved",
}


def test_no_connector_store_call_inline_in_async_handlers():
    hits = []
    for f in sorted(Path("app/web").rglob("*.py")):
        tree = ast.parse(f.read_bytes())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            awaited = {id(c) for a in ast.walk(fn) if isinstance(a, ast.Await) for c in ast.walk(a)}
            for n in ast.walk(fn):
                if not isinstance(n, ast.Call) or id(n) in awaited:
                    continue
                fnc = n.func
                if (
                    isinstance(fnc, ast.Attribute)
                    and isinstance(fnc.value, ast.Name)
                    and fnc.value.id == "conn_store"
                    and fnc.attr in _CONN_NETWORK
                ):
                    hits.append(f"{f.as_posix()}:{n.lineno} {fn.name} -> conn_store.{fnc.attr}")
                if getattr(fnc, "id", "") == "prepare_translate_locale":
                    hits.append(f"{f.as_posix()}:{n.lineno} {fn.name} -> prepare_translate_locale")
    assert not hits, "connector/seed work inline on the event loop: " + "; ".join(hits)
    print("  ratchet: no connector store network call inline in an async handler OK")


def test_eval_levenshtein_bounded():
    """Evaluations Lab edit metrics ran a full O(n*m) rapidfuzz pass (GIL held) per item."""
    from rapidfuzz.distance import Levenshtein

    from app.services import eval_metrics, postedit

    big = 120_000
    a, b = "x" * big, "y" * big
    t = time.perf_counter()
    r1 = eval_metrics._edit_char(None, a, b, {})
    r2 = eval_metrics._edit_pct(None, a, b, {})
    r3 = eval_metrics._edit_word(None, a + " z", b + " z", {})
    dt = time.perf_counter() - t
    assert dt < 3.0, f"eval edit metrics on 2 x {big} chars: {dt:.2f} s"
    assert r1.detail.get("lower_bound") is True and 0 < r2.value <= 100, (r1, r2)
    assert r3.value == 1 and not r3.detail.get("lower_bound"), r3  # one word token each, differ
    # a small edit of a huge text stays EXACT
    c = "a" * big
    r = eval_metrics._edit_char(None, c, c + " x", {})
    assert r.value == 2 and not r.detail.get("lower_bound"), r
    # everyday text: byte-identical to the full computation, and to the post-edit twin
    for p, q in (("Hello world", "Hallo Welt"), ("", "x"), ("abc " * 500, "abd " * 500)):
        assert eval_metrics._edit_char(None, p, q, {}).value == Levenshtein.distance(p, q)
        assert (
            eval_metrics._edit_pct(None, p, q, {}).value
            == Levenshtein.normalized_distance(p, q) * 100.0
        )
        assert postedit.distances(p, q)[0] == eval_metrics._edit_char(None, p, q, {}).value
    print(
        "  eval_metrics: 120k-char edit metrics bounded, small edits exact, parity with postedit OK"
    )


def main():
    test_cluster_check_unit()
    test_no_blocking_call_in_async_handlers()
    test_no_connector_store_call_inline_in_async_handlers()
    test_eval_levenshtein_bounded()
    test_translate_text_refuses_floods()
    test_mcp_translate_text_refusal()
    test_health_answers_while_engine_busy()
    test_postedit_distance_bounded()
    test_tm_fuzzy_bounded()
    test_health_answers_during_huge_editor_save()
    test_every_surface_within_budget()
    print("PATHOLOGICAL TEXT 599 OK")


if __name__ == "__main__":
    main()
