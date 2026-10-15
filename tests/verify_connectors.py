"""Connector framework verification — fully offline via httpx.MockTransport.

- Airtable + Jira connectors: test(), pull(), push() against a fake API
- store: encrypted credentials round-trip; pull materializes a Localization OS
  project + document + segments; push reads translated segments back out
- routes render; creating a connection encrypts the secret
- inbound API enforces the X-Loc-Token gate
"""

import json
import os
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_test_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app, connector_api_token
from app.models import Connection, Document, Job, JobStatus, Segment
from app.services import jobs, security, settings
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors.airtable_part2 import AirtableConnector
from app.services.connectors.base import ConnectorError
from app.services.connectors.confluence import ConfluenceConnector
from app.services.connectors.jira_part2 import JiraConnector
from tests._confluence_fake import FakeConfluence
from tests._csrf import csrf_for


def wait_job(c, jid, timeout=30):
    deadline = time.time() + timeout
    d = {}
    while time.time() < deadline:
        d = c.get(f"/jobs/{jid}/status").json()
        if d.get("status") in (JobStatus.COMPLETED, JobStatus.FAILED):
            return d
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not finish in {timeout}s: {d}")


def loc_job_id(location: str) -> int:
    """Job id out of a redirect Location — digits-only regex, NEVER int() on the
    raw tail: the dedup-message variant appends &msg=... after the id, and a
    naive int(location.rsplit('job=')[-1]) crashes on it."""
    m = re.search(r"[?&]job=(\d+)", location)
    assert m, f"no job id in Location: {location}"
    return int(m.group(1))


def drain_jobs(timeout=15) -> None:
    """Block until NO connector_translate job is QUEUED/RUNNING.

    Every section that asserts fresh-submit behavior (a 202/303 that must create
    a NEW job, or a seed sync that must be freshly created) calls this first —
    otherwise a still-active job from an earlier section makes submit_unique
    dedupe unexpectedly and the redirect grows the &msg=already-running suffix."""
    deadline = time.time() + timeout
    live = []
    while time.time() < deadline:
        with Session(engine) as s:
            live = s.exec(
                select(Job).where(
                    Job.kind == "connector_translate",
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
            ).all()
        if not live:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"connector jobs still active after {timeout}s: {[(j.id, j.status) for j in live]}"
    )


# ---- fake external APIs ----
def airtable_transport(pushed: list):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/v0/meta/bases":
            return httpx.Response(200, json={"bases": [{"id": "appX", "name": "Marketing"}]})
        if req.method == "GET" and path.startswith("/v0/appMARKETING/"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {"id": "rec1", "fields": {"Body (EN)": "Hello world"}},
                        {"id": "rec2", "fields": {"Body (EN)": "Goodbye"}},
                    ]
                },
            )
        if req.method == "PATCH" and path.startswith("/v0/appMARKETING/"):
            body = json.loads(req.content)
            pushed.extend(body["records"])
            return httpx.Response(200, json={"records": body["records"]})
        return httpx.Response(404, json={"error": "unexpected"})

    return httpx.MockTransport(handler)


def dynamic_airtable_transport(pushed: list):
    """Fake base with two target-language columns, for the convention webhook."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/v0/meta/bases/appDYN/tables":
            return httpx.Response(
                200,
                json={
                    "tables": [
                        {
                            "id": "tbl1",
                            "name": "Content",
                            "fields": [
                                {"name": "Notes"},
                                {"name": "Notes (es-MX)"},
                                {"name": "Notes (de-DE)"},
                                {"name": "Status"},
                            ],
                        }
                    ]
                },
            )
        if req.method == "GET" and path.startswith("/v0/appDYN/"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {"id": "recA", "fields": {"Notes": "Hello world"}},
                        {"id": "recB", "fields": {"Notes": "Goodbye"}},
                    ]
                },
            )
        if req.method == "PATCH" and path.startswith("/v0/appDYN/"):
            body = json.loads(req.content)
            pushed.extend(body["records"])
            return httpx.Response(200, json={"records": body["records"]})
        return httpx.Response(404, json={"error": "unexpected", "path": path})

    return httpx.MockTransport(handler)


def jira_transport(pushed: list):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"displayName": "Alex"})
        if path == "/rest/api/3/project/search":
            return httpx.Response(200, json={"values": [{"key": "LOC", "name": "Localization"}]})
        if path == "/rest/api/3/search/jql":
            return httpx.Response(
                200,
                json={
                    "isLast": True,
                    "issues": [{"key": "LOC-1", "fields": {"summary": "Need translation"}}],
                },
            )
        if path.endswith("/comment") and req.method == "POST":
            pushed.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "10001"})
        return httpx.Response(404, json={"error": "unexpected"})

    return httpx.MockTransport(handler)


def jira_translate_transport(posted: list):
    """Jira with a summary + ADF description, capturing posted comments."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/rest/api/3/search/jql":
            return httpx.Response(
                200,
                json={
                    "isLast": True,
                    "issues": [
                        {
                            "key": "LOC-9",
                            "fields": {
                                "summary": "Reset your password",
                                "description": {
                                    "type": "doc",
                                    "version": 1,
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [
                                                {
                                                    "type": "text",
                                                    "text": "Click the link in the email.",
                                                }
                                            ],
                                        }
                                    ],
                                },
                            },
                        }
                    ],
                },
            )
        if path.endswith("/comment") and req.method == "POST":
            posted.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "1"})
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def confluence_tenant(body="<h1>Reset password</h1><p>Click the link.</p>"):
    """A stateful fake Confluence tenant (REST v2): space HELP with source page 100. Its
    `.transport()` serves the connector; `.pages` is the real state to assert on."""
    t = FakeConfluence()
    t.add_space("HELP", "Help Center")
    t.add_page("HELP", "Reset password", body, pid="100")
    return t


def response_limits():
    """#269: cap streamed success/error bodies and stop slow trickles without retries."""
    from unittest.mock import patch

    from app.services.connectors import base

    class Body(httpx.SyncByteStream):
        closed = False
        reads = 0

        def __iter__(self):
            for _ in range(20):
                self.reads += 1
                yield b"12345678"

        def close(self):
            self.closed = True

    for status in (200, 429, 500):
        for slow in (False, True):
            body = Body()
            requests = []

            def handle(request, requests=requests, status=status, body=body):
                requests.append(request)
                return httpx.Response(status, stream=body)

            conn = AirtableConnector({}, {}, transport=httpx.MockTransport(handle))
            with (
                patch.object(base, "RESPONSE_MAX_BYTES", 16, create=True),
                patch.object(base, "RESPONSE_DEADLINE_S", 5, create=True),
                patch.object(
                    base,
                    "_monotonic",
                    side_effect=lambda body=body, slow=slow: body.reads * 2 if slow else 0,
                    create=True,
                ),
                conn._client("https://api.airtable.com") as client,
            ):
                try:
                    conn._request(client, "GET", "/")
                except base.ConnectorError as exc:
                    assert ("too long" if slow else "too large") in str(exc), str(exc)
                else:
                    raise AssertionError("Unbounded connector response accepted")
            assert body.closed and body.reads < 20
            assert len(requests) == 1, "Limit failures must not retry"
    print("  response byte cap and deadline: success, rate limit and server error")

    import gzip

    for payload, accepted in ((b'{"ok":true}', True), (b"x" * 10000, False)):
        compressed = gzip.compress(payload)

        class Compressed(httpx.SyncByteStream):
            closed = False

            def __iter__(self, compressed=compressed):
                yield compressed

            def close(self):
                self.closed = True

        body = Compressed()
        conn = AirtableConnector(
            {},
            {},
            transport=httpx.MockTransport(
                lambda request, body=body: httpx.Response(
                    200, stream=body, headers={"Content-Encoding": "gzip"}
                )
            ),
        )
        with (
            patch.object(base, "RESPONSE_MAX_BYTES", 100),
            conn._client("https://api.airtable.com") as client,
        ):
            try:
                response = conn._request(client, "GET", "/")
            except base.ConnectorError:
                assert not accepted
            else:
                assert accepted and response.json() == {"ok": True}
        assert body.closed


def response_round_one():
    """F12: exercise all gate regressions before reporting failures."""
    import gzip
    import zlib
    from unittest.mock import patch

    from app.services.connectors.wordpress import WordPressConnector

    failures = []
    creds = {
        "api_key": "fixture",  # pregate: allow-secret fixture
        "username": "fixture",
        "application_password": "fixture",  # pregate: allow-secret fixture
        "email": "fixture@example.com",
        "api_token": "fixture",  # pregate: allow-secret fixture
    }

    def check(name, fn):
        try:
            fn()
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  F12 {name}: PASS")

    def retries(wait, slow=False, sleep_cap=120):
        now = [0.0]
        sleeps, hits = [], []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        def handle(req):
            hits.append(req)
            now[0] += 16 if slow else 1
            status = (
                500 if slow else (429 if len(hits) == 1 or wait > 60 or sleep_cap < 120 else 200)
            )
            return httpx.Response(status, headers={"Retry-After": str(wait)}, json={})

        conn = AirtableConnector({}, {}, transport=httpx.MockTransport(handle))
        with (
            patch.object(cbase, "_monotonic", side_effect=lambda: now[0]),
            patch.object(cbase, "_sleep", side_effect=sleep),
            patch.object(cbase, "MAX_TOTAL_SLEEP", sleep_cap),
            conn._client("https://api.airtable.com") as client,
        ):
            try:
                response = conn._request(client, "GET", "/")
            except ConnectorError as exc:
                assert slow and "too long" in str(exc), str(exc)
                assert len(hits) == 2 and sleeps == [wait], (len(hits), sleeps)
            else:
                assert not slow, "Retries reset the I/O budget"
                assert response.status_code == (429 if wait > 60 or sleep_cap < 120 else 200)
                expected = [60.0, 60.0] if wait > 60 else [wait]
                assert sleeps == expected, sleeps
                assert len(hits) == (3 if wait > 60 else 2), hits

    # Two 16 s attempts exceed 30 s even though each attempt fits alone.
    check("Retry-After 30", lambda: retries(30))
    check("Retry-After 60", lambda: retries(60))
    check("Retry-After 120 capped rate limit", lambda: retries(120))
    check("total sleep cap returns provider 429", lambda: retries(40, sleep_cap=60))
    check("shared I/O across sleeps and two 500s", lambda: retries(1, slow=True))

    def caps():
        assert cbase.RESPONSE_MAX_BYTES == 32 * 1024 * 1024
        large = b" " * (9 * 1024 * 1024) + b'{"bases":[{"id":"app1","name":"Base"}]}'

        def handle(req):
            if req.url.path.endswith("/tables"):
                return httpx.Response(200, json={"tables": []})
            return httpx.Response(200, content=large)

        transport = httpx.MockTransport(handle)
        assert cstore.browse_containers_unsaved("airtable", {}, creds, transport=transport) == []
        conn = AirtableConnector({}, creds, transport=transport)
        assert len(conn.containers()) == 1

    check("saved and unsaved caps", caps)

    def compressed(encoding, payload):
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                # Header split across chunks exercises raw-deflate fallback buffering.
                yield payload[:1]
                yield payload[1:]

        conn = AirtableConnector(
            {},
            {},
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200, stream=Stream(), headers={"Content-Encoding": encoding}
                )
            ),
        )
        for limit, accepted in ((100, True), (6, False)):
            with (
                patch.object(cbase, "RESPONSE_MAX_BYTES", limit),
                conn._client("https://api.airtable.com") as client,
            ):
                try:
                    result = conn._request(client, "GET", "/")
                except ConnectorError as exc:
                    assert not accepted and "too large" in str(exc), str(exc)
                else:
                    assert accepted and result.json() == {"ok": 1}

    raw = zlib.compressobj(wbits=-15)
    raw = raw.compress(b'{"ok":1}') + raw.flush()
    check("raw deflate", lambda: compressed("deflate", raw))
    members = gzip.compress(b'{"ok":') + gzip.compress(b"1}")
    check("multi-member gzip", lambda: compressed("gzip", members))

    def pages(kind, always_large=False, fetch=False):
        seen = []

        def handle(req):
            params = dict(req.url.params)
            if kind == "confluence":
                path = req.url.path
                if path == "/wiki/api/v2/spaces":
                    return httpx.Response(
                        200, json={"results": [{"id": "1", "key": "HELP", "name": "Help"}]}
                    )
                if path.startswith("/wiki/api/v2/pages/"):
                    return httpx.Response(
                        200, json={"id": path.rsplit("/", 1)[-1], "title": "T", "spaceId": "1"}
                    )
            seen.append(params)
            size_key = {"airtable": "pageSize", "wordpress": "per_page", "confluence": "limit"}[
                kind
            ]
            size = int(params[size_key])
            if kind == "airtable":
                assert req.url.params.get_list("fields[]") == ["Source", "Target", "Title"]
            if size == 4 or always_large:
                return httpx.Response(200, content=b"x" * 1025)
            if kind == "airtable":
                offset = int(params.get("offset", "0"))
                body = {
                    "records": [
                        {"id": str(i), "fields": {"Source": "Hi"}}
                        for i in range(offset, offset + 2)
                    ]
                }
                if offset == 0:
                    body["offset"] = "2"
                return httpx.Response(200, json=body)
            if kind == "wordpress":
                offset = int(params.get("offset", (int(params.get("page", 1)) - 1) * size))
                return httpx.Response(
                    200,
                    json=[{"id": i} for i in range(offset, offset + 2)],
                    headers={"X-WP-TotalPages": "2"},
                )
            if fetch:  # v1 CQL search: ids only, offset paging with `start`
                start = int(params.get("start", 0))
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"content": {"id": str(i), "type": "page"}}
                            for i in range(start, start + 2)
                        ],
                        "_links": {"next": "ignored"} if start == 0 else {},
                    },
                )
            start = int(params.get("cursor", 0))  # v2 space listing: opaque cursor
            return httpx.Response(
                200,
                json={
                    "results": [{"id": str(i), "title": "T"} for i in range(start, start + 2)],
                    "_links": {"next": "/wiki/api/v2/spaces/1/pages?cursor=2"}
                    if start == 0
                    else {},
                },
            )

        transport = httpx.MockTransport(handle)
        with patch.object(cbase, "RESPONSE_MAX_BYTES", 1024):
            try:
                if kind == "airtable":
                    conn = AirtableConnector(
                        {
                            "base_id": "app1",
                            "table": "Table",
                            "source_field": "Source",
                            "target_field": "Target",
                            "title_field": "Title",
                            "max_records": 4,
                        },
                        creds,
                        transport=transport,
                    )
                    if fetch:
                        result = conn.fetch("app1", "Table", cap=4)
                        ids = [item["id"] for item in result]
                    else:
                        result = conn.pull()
                        ids = [item.external_id for item in result]
                elif kind == "wordpress":
                    conn = WordPressConnector(
                        {"base_url": "https://wp.example.com"}, creds, transport=transport
                    )
                    with conn._client() as client:
                        result = conn._paginate(client, "posts", {"context": "edit"}, 4)
                    ids = [str(item["id"]) for item in result]
                else:
                    conn = ConfluenceConnector(
                        {"base_url": "https://wiki.example.com"}, creds, transport=transport
                    )
                    result = conn.discover_articles(
                        space_key="HELP", cql="type = page" if fetch else None, cap=4
                    )
                    ids = [item["id"] for item in result]
            except ConnectorError as exc:
                assert always_large and "smaller page" in str(exc), str(exc)
                assert len(seen) == 2, seen
            else:
                assert not always_large
                assert ids == ["0", "1", "2", "3"], ids
                assert len(seen) == 3, seen

    for kind in ("airtable", "wordpress", "confluence"):
        check(f"{kind} halve page", lambda kind=kind: pages(kind))
        check(f"{kind} smaller page refusal", lambda kind=kind: pages(kind, True))
    check("airtable fetch fields and halve page", lambda: pages("airtable", fetch=True))
    check("confluence search halve page", lambda: pages("confluence", fetch=True))
    assert not failures, "\n".join(failures)


def response_socket_deadline():
    """A slow HTTP header never yields body chunks; budget each socket read too."""
    from unittest.mock import patch

    from app.services.connectors import base

    class Socket:
        reads = 0
        closed = False

        def read(self, max_bytes, timeout=None):
            assert 0 < timeout <= 5 - self.reads * 2
            self.reads += 1
            return b"HTTP/1.1 200 OK\r\n" if self.reads == 1 else b"X-Slow: yes\r\n"

        def write(self, buffer, timeout=None):
            assert timeout <= 5

        def close(self):
            self.closed = True

        def get_extra_info(self, info):
            if info == "server_addr":
                return ("93.184.216.34", 80)
            return None

    sock = Socket()

    class Backend:
        def connect_tcp(self, *args, **kwargs):
            assert kwargs["timeout"] <= 5
            return sock

    transport = httpx.HTTPTransport()
    transport._pool._network_backend = Backend()
    conn = AirtableConnector({}, {}, transport=transport)
    with (
        patch.object(base, "RESPONSE_DEADLINE_S", 5),
        patch.object(base, "_monotonic", side_effect=lambda: sock.reads * 2),
        conn._client("http://api.airtable.com") as client,
    ):
        try:
            conn._request(client, "GET", "/")
        except base.ConnectorError as exc:
            assert "too long" in str(exc)
        else:
            raise AssertionError("Slow response headers escaped the deadline")
    assert sock.closed and sock.reads == 3


def flash_redirect_encoding() -> None:
    """#501: a connector action redirect must percent-encode the flash msg.

    A provider error carrying '&', '#', '+' or '%xx' corrupts the query when
    interpolated raw: a '#' truncates everything after it and a stray '&ok=1'
    inside the text adds a SECOND 'ok' that Starlette's QueryParams.get returns,
    so a failure would render as a green success banner with a cut-off message.
    This drives the REAL /test and /rotate routes (only the store call is stubbed,
    to keep it offline) and parses the redirect Location: exactly one ok=0 and the
    full msg. A plain message must still round-trip.
    """
    from urllib.parse import parse_qsl, urlsplit

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    settings.set_many({"security_auth_required": "0"})
    c = TestClient(fastapi_app)
    evil = "HTTP 400: bad field &ok=1 #x"
    conn = cstore.create(
        "airtable",
        "Flash encode",
        {"base_id": "appX", "table": "T", "source_field": "S", "target_field": "D"},
        {"api_key": "patFLASH"},  # pregate: allow-secret fixture
        actor="admin",
    )

    def qsl(location: str) -> dict:
        out = {}
        for k, v in parse_qsl(urlsplit(location).query):
            out.setdefault(k, []).append(v)
        return out

    def check_redirect(route: str, data, where: str) -> None:
        r = c.post(route, data=data, follow_redirects=False)
        assert r.status_code == 303, (where, r.status_code)
        loc = r.headers["location"]
        q = qsl(loc)
        assert q.get("ok") == ["0"], f"{where}: want one ok=0, got {q.get('ok')} in {loc!r}"
        assert q.get("msg") == [evil], f"{where}: msg must round-trip, got {q.get('msg')} {loc!r}"

    # (1) test route: the failure msg's '&'/'#' must not forge a second ok.
    saved_test = cstore.test_connection
    set_pieces(
        "app.services.connectors.store",
        "test_connection",
        lambda cid, actor="", **kw: (False, evil),
    )
    try:
        check_redirect(f"/connectors/connection/{conn.id}/test", {}, "test")
    finally:
        set_pieces("app.services.connectors.store", "test_connection", saved_test)

    # (2) rotate route: same guarantee on a second (independent) route.
    saved_rotate = cstore.rotate_secret
    set_pieces(
        "app.services.connectors.store",
        "rotate_secret",
        lambda cid, secret, actor="", **kw: (False, evil),
    )
    try:
        check_redirect(f"/connectors/connection/{conn.id}/rotate", {"api_key": "patNEW"}, "rotate")
    finally:
        set_pieces("app.services.connectors.store", "rotate_secret", saved_rotate)

    # (3) a plain message still round-trips (no regression on the common path).
    saved_test = cstore.test_connection
    set_pieces(
        "app.services.connectors.store",
        "test_connection",
        lambda cid, actor="", **kw: (True, "Connected."),
    )
    try:
        r = c.post(f"/connectors/connection/{conn.id}/test", follow_redirects=False)
        assert r.status_code == 303, r.status_code
        q = qsl(r.headers["location"])
        assert q.get("ok") == ["1"], q
        assert q.get("msg") == ["Connected."], q
    finally:
        set_pieces("app.services.connectors.store", "test_connection", saved_test)

    print("  flash #501: test + rotate redirects encode msg (one ok, full text; plain round-trips)")


def main() -> None:
    init_db()
    response_limits()
    response_socket_deadline()
    response_round_one()

    # ---- Airtable connector (direct) ----
    pushed_at = []
    cfg = {
        "base_id": "appMARKETING",
        "table": "Content",
        "source_field": "Body (EN)",
        "target_field": "Body (FR)",
        "target_locale": "fr-FR",
    }
    at = AirtableConnector(cfg, {"api_key": "patTEST"}, transport=airtable_transport(pushed_at))
    ok, msg = at.test()
    assert ok, msg
    items = at.pull()
    assert [i.text for i in items] == ["Hello world", "Goodbye"], items
    assert items[0].external_id == "rec1" and items[0].field == "Body (FR)"
    for it in items:
        it.text = "[FR] " + it.text
    n, _ = at.push(items)
    assert n == 2 and pushed_at[0]["fields"]["Body (FR)"] == "[FR] Hello world", pushed_at
    assert at.containers()[0]["name"] == "Marketing"
    print("  airtable: test + pull(2) + push(2) + list bases")

    # ---- Jira connector (direct) ----
    pushed_j = []
    jcfg = {
        "base_url": "https://team.atlassian.net",
        "jql": "project = LOC",
        "source_field": "summary",
        "target_locale": "fr-FR",
    }
    jira = JiraConnector(
        jcfg, {"email": "b@x.com", "api_token": "tok"}, transport=jira_transport(pushed_j)
    )
    ok, msg = jira.test()
    assert ok, msg
    jitems = jira.pull()
    assert jitems[0].external_id == "LOC-1" and jitems[0].text == "Need translation"
    jitems[0].text = "Besoin de traduction"
    n, _ = jira.push(jitems)
    assert n == 1, n
    # the ADF comment carries the translated text
    adf = pushed_j[0]["body"]
    assert adf["content"][0]["content"][0]["text"] == "Besoin de traduction", adf
    assert jira.containers()[0]["id"] == "LOC"
    print("  jira: test + pull(1) + push(1 comment) + list projects")

    # ---- store: encrypted creds + pull->LS content + push ----
    conn = cstore.create("airtable", "Marketing base", cfg, {"api_key": "patSECRET"}, actor="admin")
    with Session(engine) as s:
        row = s.get(Connection, conn.id)
    assert "patSECRET" not in row.secret_enc, "secret stored in plaintext!"
    assert cstore.dec_secret(row.secret_enc)["api_key"] == "patSECRET", "secret did not round-trip"
    print("  store: credentials encrypted at rest + decrypt round-trip")

    ok, msg, doc_id = cstore.pull_connection(
        conn.id, actor="admin", transport=airtable_transport([])
    )
    assert ok and doc_id, msg
    with Session(engine) as s:
        conn2 = s.get(Connection, conn.id)
        assert conn2.project_id, "pull should create a project"
        doc = s.get(Document, doc_id)
        assert doc.fmt == "airtable" and doc.skeleton["units"][0]["ext_id"] == "rec1"
        segs = sorted(
            s.exec(select(Segment).where(Segment.document_id == doc_id)).all(),
            key=lambda x: x.order_index,
        )
        assert [sg.source_text for sg in segs] == ["Hello world", "Goodbye"], segs
        # simulate translation
        for sg in segs:
            sg.target_text = "[FR] " + sg.source_text
            s.add(sg)
        s.commit()
    print(f"  store: pull created project + document #{doc_id} + 2 segments")

    pushed_back = []
    ok, msg, n = cstore.push_connection(
        conn.id, actor="admin", transport=airtable_transport(pushed_back)
    )
    assert ok and n == 2, (ok, msg, n)
    assert pushed_back[0]["fields"]["Body (FR)"] == "[FR] Hello world", pushed_back
    print("  store: push wrote 2 translated segments back to Airtable")

    # ---- store: automated round-trip (pull -> MT -> push) in one call ----
    # MT runs on the offline MockProvider (no API keys in tests), so this is hermetic.
    synced = []
    ok, msg, summary = cstore.sync_connection(
        conn.id, actor="auto", transport=airtable_transport(synced)
    )
    assert ok, msg
    assert summary["translated"] == 2 and summary["pushed"] == 2, summary
    assert all((r["fields"].get("Body (FR)") or "").strip() for r in synced), synced
    print(
        f"  store: automated sync translated {summary['translated']} + pushed {summary['pushed']} (no human step)"  # noqa: E501
    )

    # translate_now on an Airtable connection missing base/table/source falls back to
    # the single-locale round-trip (sync_connection), message passed through UNCHANGED
    # (no decoration -- jobs/API consumers surface this string verbatim)
    nodyn = cstore.create(
        "airtable",
        "No dynamic cfg",
        {"source_field": "Body (EN)"},
        {"api_key": "patX"},
        actor="admin",
    )
    okfb, msg_fb, sfb = cstore.translate_now(
        nodyn.id, actor="auto", transport=airtable_transport([])
    )
    assert not okfb and msg_fb.startswith("Pull step:"), (okfb, msg_fb)
    assert "document_id" in sfb, sfb  # sync-shaped summary proves the fallback path ran
    print(
        "  translate_now: missing base/table/source falls back to sync_connection (msg untouched)"
    )

    # ---- convention-driven webhook: auto-detect target columns, multi-locale ----
    from app.services.connectors.airtable import detect_targets

    tgts = detect_targets("Notes", ["Notes", "Notes (es-MX)", "Notes (de-DE)", "Status"])
    assert tgts == [("Notes (es-MX)", "es-MX"), ("Notes (de-DE)", "de-DE")], tgts

    dyn = cstore.create(
        "airtable",
        "Dynamic base",
        {
            "source_field": "Notes",
            "naming_pattern": "{src} ({loc})",
            "source_locale": "en-US",
            # #419: a base/table override must match the configured scope
            "base_id": "appDYN",
            "table": "Content",
        },
        {"api_key": "patDYN"},
        actor="admin",
    )
    wh_pushed = []
    ok, msg, summary = cstore.dynamic_translate(
        dyn.id,
        base_id="appDYN",
        table="Content",
        source_field=None,
        jobs=None,
        actor="airtable-automation",
        transport=dynamic_airtable_transport(wh_pushed),
    )
    assert ok, msg
    assert summary["targets"] == 2 and summary["records"] == 2, summary
    assert summary["translated"] == 4 and summary["pushed"] == 2, summary  # 2 records x 2 locales
    by_id = {r["id"]: r["fields"] for r in wh_pushed}
    assert (by_id["recA"].get("Notes (es-MX)") or "").strip(), by_id
    assert (by_id["recA"].get("Notes (de-DE)") or "").strip(), by_id  # merged in one PATCH
    print(
        "  webhook: auto-detected 2 target columns, translated 2 records x 2 locales, merged write"
    )

    # ---- Jira: multi-locale on-the-fly translate -> labelled comments ----
    jconn = cstore.create(
        "jira",
        "Jira tickets",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary,description",
            "target_locales": "es-MX, de-DE",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    posted = []
    ok, msg, summary = cstore.jira_translate(
        jconn.id, actor="auto", transport=jira_translate_transport(posted)
    )
    assert ok, msg
    assert summary["issues"] == 1 and summary["written"] == 2, summary  # 1 issue × 2 locales
    blob = json.dumps(posted)
    assert "es-MX" in blob and "de-DE" in blob, "comment labels missing locale"
    assert len(posted) == 2, posted
    print(f"  jira_translate: 1 issue -> {summary['written']} labelled comments (es-MX, de-DE)")

    # ---- Confluence: translate page -> localized child pages (via HTML filter) ----
    cconn = cstore.create(
        "confluence",
        "Help center",
        {
            "base_url": "https://team.atlassian.net",
            "space_key": "HELP",
            "target_locales": "es-MX, de-DE",
            "source_locale": "en-US",
            "title_pattern": "[{loc}] {title}",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    cft = confluence_tenant()
    okk, _m = cstore.test_connection(cconn.id, transport=cft.transport())
    assert okk, _m
    ok, msg, summary = cstore.confluence_translate(
        cconn.id, actor="auto", transport=cft.transport()
    )
    assert ok, msg
    assert summary["pages"] == 1 and summary["created"] == 2, summary  # 1 page x 2 locales
    kids = cft.children("100")
    titles = [c["title"] for c in kids]
    assert any(t.startswith("[es-MX]") for t in titles) and any(
        t.startswith("[de-DE]") for t in titles
    ), titles
    for c in kids:
        assert "<h1>" in c["body"] and "<p>" in c["body"], c["body"]  # markup preserved
    print(f"  confluence_translate: 1 page -> {summary['created']} child pages, markup preserved")

    # re-running: the id map knows both children, so nothing is created or rewritten.
    n_pages, n_writes = len(cft.pages), len(cft.writes())
    ok, msg, summary = cstore.confluence_translate(
        cconn.id, actor="auto", transport=cft.transport()
    )
    assert ok, msg
    assert summary["created"] == 0 and summary["skipped"] == 2, summary
    assert len(cft.pages) == n_pages and len(cft.writes()) == n_writes, "re-run must not write"
    print("  confluence_translate re-run: 0 created, 2 skipped (idempotent)")

    # a human page that merely shares the locale tag is NEVER overwritten: needs_link.
    cht = confluence_tenant()
    human = cht.add_page("HELP", "[es-MX] Some other doc", "<p>Humano</p>", parent="100")
    chum = cstore.create(
        "confluence",
        "Help human",
        {
            "base_url": "https://team.atlassian.net",
            "space_key": "HELP",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    ok, msg, summary = cstore.confluence_translate(
        chum.id, ["100"], actor="auto", transport=cht.transport()
    )
    assert summary["created"] == 0 and cht.pages[human]["body"] == "<p>Humano</p>", summary
    assert not cht.writes(), "an unrecorded page with our locale tag must not be touched"
    print("  confluence_translate: a human page with our locale tag -> needs_link, untouched")

    # a SUFFIX-style title pattern ({loc} after {title}) also lands and stays idempotent
    csuf = cstore.create(
        "confluence",
        "Help suffix",
        {
            "base_url": "https://team.atlassian.net",
            "space_key": "HELP",
            "target_locales": "es-MX, de-DE",
            "source_locale": "en-US",
            "title_pattern": "{title} ({loc})",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    cfs = confluence_tenant()
    ok, msg, summary = cstore.confluence_translate(csuf.id, actor="auto", transport=cfs.transport())
    assert ok and summary["created"] == 2, (msg, summary)
    suf_titles = [c["title"] for c in cfs.children("100")]
    assert all(t.endswith("(es-MX)") or t.endswith("(de-DE)") for t in suf_titles), suf_titles
    ok, msg, summary = cstore.confluence_translate(csuf.id, actor="auto", transport=cfs.transport())
    assert ok, msg
    assert summary["created"] == 0 and summary["skipped"] == 2, summary
    print("  confluence_translate suffix pattern: re-run 0 created, 2 skipped (idempotent)")

    # ---- field-mapping pickers: store wrappers (schema_fields, unsaved browse) ----
    # schema_fields: happy path returns the table's field names (Airtable meta API).
    def at_schema_transport():
        def handler(req):
            path = req.url.path
            if req.method == "GET" and path == "/v0/meta/bases/appDYN/tables":
                return httpx.Response(
                    200,
                    json={
                        "tables": [
                            {
                                "id": "tbl1",
                                "name": "Content",
                                "fields": [
                                    {"name": "Notes"},
                                    {"name": "Notes (es-MX)"},
                                    {"name": "Status"},
                                ],
                            }
                        ]
                    },
                )
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    sf_conn = cstore.create(
        "airtable",
        "Schema fields",
        {"source_field": "Notes", "base_id": "appDYN", "table": "Content"},  # #419 scope
        {"api_key": "patSF"},
        actor="admin",
    )
    fields = cstore.schema_fields(sf_conn.id, "appDYN", "Content", transport=at_schema_transport())
    assert fields == ["Notes", "Notes (es-MX)", "Status"], fields
    # wrong kind -> [] (only Airtable exposes a schema); missing connection -> []
    wk_conn = cstore.create(
        "jira",
        "Not airtable",
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "t"},
        actor="admin",
    )
    assert cstore.schema_fields(wk_conn.id, "appX", "T") == [], "wrong kind must return []"
    assert cstore.schema_fields(99999999, "appX", "T") == [], "missing conn must return []"
    # ConnectorError propagates (unknown table -> raised by AirtableConnector.schema_fields)
    raised = False
    try:
        cstore.schema_fields(sf_conn.id, "appDYN", "Nope", transport=at_schema_transport())
    except cbase.ConnectorError:
        raised = True
    assert raised, "schema_fields must let ConnectorError propagate for a bad table"
    print("  store.schema_fields: happy (3 fields) + wrong-kind/[] + ConnectorError propagates")

    # browse_containers_unsaved: build a THROWAWAY connector from typed creds, per kind.
    # A nested airtable transport: /meta/bases lists a base, and its /tables lists tables.
    def at_bases_transport():
        def handler(req):
            path = req.url.path
            if req.method == "GET" and path == "/v0/meta/bases":
                return httpx.Response(200, json={"bases": [{"id": "appX", "name": "Marketing"}]})
            if req.method == "GET" and path == "/v0/meta/bases/appX/tables":
                return httpx.Response(200, json={"tables": [{"id": "tbl1", "name": "Content"}]})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    unsaved_at = cstore.browse_containers_unsaved(
        "airtable", {}, {"api_key": "patTMP"}, transport=at_bases_transport()
    )
    assert unsaved_at and unsaved_at[0]["id"] == "appX", unsaved_at
    assert unsaved_at[0]["tables"][0]["name"] == "Content", unsaved_at  # nested tables
    unsaved_jira = cstore.browse_containers_unsaved(
        "jira",
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "t"},
        transport=jira_transport([]),
    )
    assert unsaved_jira and unsaved_jira[0]["id"] == "LOC", unsaved_jira
    unsaved_conf = cstore.browse_containers_unsaved(
        "confluence",
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "t"},
        transport=confluence_tenant().transport(),
    )
    assert unsaved_conf and unsaved_conf[0]["id"] == "HELP", unsaved_conf
    assert cstore.browse_containers_unsaved("nope", {}, {}) == [], "unknown kind -> []"
    print(
        "  store.browse_containers_unsaved: airtable bases + jira projects + confluence spaces; bad kind []"  # noqa: E501
    )

    # ---- SSRF guard: base._validate_target on every live-network path ----
    # (a) the validator itself: bad scheme + private/metadata IP literals raise;
    #     public https hosts (the maintainer's real Jira + Airtable) pass through unchanged.
    from app.services import settings as _settings_default

    # The LLM-layer LAN address is no longer a shipped default (empty since 2026-10-06), so the
    # connector SSRF guard has no special case for it: it is just another private host.
    assert _settings_default.DEFAULTS["local_base_url"] == ""
    for bad in (
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1/admin",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://192.168.1.50:1234/v1",  # a private host (not the LLM layer)
        "http://172.16.0.1/",
        "http://0.0.0.0/",
        "file:///etc/passwd",
        "gopher://evil/",
        "ftp://internal/",
    ):
        raised = False
        try:
            cbase._validate_target(bad)
        except cbase.ConnectorError:
            raised = True
        assert raised, f"must block SSRF target: {bad}"
    for good in (
        "https://example-co.atlassian.net",  # real Jira/Confluence site
        "https://api.airtable.com/v0",  # real Airtable host
        "https://team.atlassian.net/wiki",  # a DNS name (not resolved)
        "https://example.com/path",
    ):
        assert cbase._validate_target(good) == good, f"public host must pass: {good}"

    # ---- P-F1: alternate IP encodings + internal names (SSRF guard bypass regression) ----
    # Each of these WAS allowed (fell through to `return raw` as a "hostname") before P-F1.
    # glibc's inet_aton / getaddrinfo resolves the numeric forms to loopback/metadata at
    # connect time, so they must be canonicalized and range-checked; the name forms are
    # refused by literal. ALL of these hold OFFLINE (no resolver installed).
    _pf1_blocked = [
        "http://2130706433/",  # decimal      -> 127.0.0.1
        "http://0x7f000001/",  # hex          -> 127.0.0.1
        "http://0177.0.0.1/",  # octal        -> 127.0.0.1
        "http://017700000001/",  # single octal -> 127.0.0.1
        "http://127.1/",  # short-form   -> 127.0.0.1
        "http://10.1/",  # short-form   -> 10.0.0.1 (private)
        "http://0x7f.1/",  # mixed hex short-form -> 127.0.0.1
        "http://2852039166/",  # decimal      -> 169.254.169.254 (metadata)
        "http://100.64.1.1/",  # CGNAT literal is_private MISSES
        "http://localhost/",  # localhost name
        "http://LOCALHOST/",  # case-insensitive
        "http://foo.internal/",  # .internal suffix (GCP metadata convention)
        "http://bar.local/",  # .local suffix (mDNS)
        "http://[::1]/",  # IPv6 loopback literal
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6 loopback
        "http://[::ffff:169.254.169.254]/",  # IPv4-mapped IPv6 metadata
        "http://localhost./",  # trailing-dot FQDN resolves == localhost
        "http://2130706433./",  # trailing-dot numeric literal
    ]
    for bad in _pf1_blocked:
        raised = False
        try:
            cbase._validate_target(bad)
        except cbase.ConnectorError:
            raised = True
        assert raised, f"P-F1: alternate-encoding/internal SSRF target must be blocked: {bad}"
    # A numeric-LOOKING but public host that is NOT a valid inet_aton form stays a hostname
    # (allowed offline): '1.2.3.4.5' has 5 parts, 'api.airtable.com' has non-numeric labels.
    for good in ("https://1.2.3.4.5/", "https://8.8.8.8/", "https://cafe.example.com/"):
        assert cbase._validate_target(good) == good, f"must stay allowed: {good}"

    # ---- P-F1: resolve-and-check via the INJECTABLE resolver seam (production posture) ----
    # Offline the resolver is None (skipped); a test installs a fake host->[ip] map so the
    # resolve-and-check path runs hermetically. A hostname that resolves to a private IP is
    # refused (split-horizon / rebind-prep); one that resolves public is allowed; a resolution
    # FAILURE is "allow, could not verify" (must never hang/hard-fail the offline suites).
    # P3 round-1 L1: assert the LIVE resolver state (egress._RESOLVER), NOT base's import snapshot.
    # base re-exports `_RESOLVER` by value once at import, so `set_test_resolver` (which mutates
    # egress._RESOLVER) leaves cbase._RESOLVER frozen at None -- the old assertion could no longer
    # CATCH a leaked resolver, which is the whole point of a start-of-test offline-default guard.
    from app.services import egress as egress_mod

    assert egress_mod._RESOLVER is None, "verify suites must start with NO resolver (offline)"
    try:
        cbase.set_test_resolver(lambda h: ["10.0.0.5"])  # private A record
        r = False
        try:
            cbase._validate_target("https://sneaky.example.com/")
        except cbase.ConnectorError:
            r = True
        assert r, "P-F1: a host resolving to a private IP must be refused"
        cbase.set_test_resolver(lambda h: ["1.2.3.4", "169.254.169.254"])  # ANY private -> reject
        r = False
        try:
            cbase._validate_target("https://mixed.example.com/")
        except cbase.ConnectorError:
            r = True
        assert r, "P-F1: refuse when ANY resolved address is private/metadata"
        cbase.set_test_resolver(lambda h: ["93.184.216.34"])  # public A record
        assert cbase._validate_target("https://real.example.com/") == "https://real.example.com/"

        def _boom(h):
            raise OSError("offline / DNS unavailable")

        cbase.set_test_resolver(_boom)  # failure -> allow, no hang
        assert cbase._validate_target("https://slow.example.com/") == "https://slow.example.com/"
    finally:
        cbase.set_test_resolver(None)  # restore offline default
    # With the resolver cleared, the always-on layers (numeric + localhost) still block.
    for bad in ("http://2130706433/", "http://localhost/"):
        raised = False
        try:
            cbase._validate_target(bad)
        except cbase.ConnectorError:
            raised = True
        assert raised, f"P-F1: numeric/localhost blocked even with no resolver: {bad}"

    # (b) end-to-end through the connectors: a Jira/Confluence base_url pointed at
    #     the metadata IP raises ConnectorError BEFORE any network call (the mock
    #     transport would 404, so reaching it would prove the guard was bypassed).
    def _never_called_transport():
        def handler(req):
            raise AssertionError(f"SSRF guard should have blocked before any request: {req.url}")

        return httpx.MockTransport(handler)

    for kind, ctor in (("jira", JiraConnector), ("confluence", ConfluenceConnector)):
        try:  # noqa: SIM105
            ctor(
                {"base_url": "http://169.254.169.254/"},
                {"email": "b@x.com", "api_token": "t"},
                transport=_never_called_transport(),
            ).test()
            # test() swallows ConnectorError into (False, msg); assert that shape too
        except cbase.ConnectorError:
            pass
        # test() catches ConnectorError and returns (False, msg) — either the raise
        # propagated or it was caught; confirm via a direct base builder call which
        # must raise regardless.
        raised = False
        try:
            c_ = ctor(
                {"base_url": "http://169.254.169.254/"}, {"email": "b@x.com", "api_token": "t"}
            )
            (c_._base if kind == "jira" else c_._site)()
        except cbase.ConnectorError:
            raised = True
        assert raised, f"{kind} base builder must reject the metadata IP"

    # (c) Airtable's fixed public host still passes (the base builder validates the
    #     constant API root); a bad scheme in the constant would raise, a good one
    #     returns the root so all its request paths keep working.
    at_ssrf = AirtableConnector({"base_id": "appX", "table": "T"}, {"api_key": "patX"})
    assert at_ssrf._api().startswith("https://api.airtable.com"), at_ssrf._api()
    print(
        "  ssrf: _validate_target blocks metadata/private IPs + bad schemes; public https (Jira/Airtable) pass"  # noqa: E501
    )

    # ---- (a) rate-limit / backoff wrapper ----
    # A 429 with a numeric Retry-After, then a 200 -> the op succeeds and the
    # backoff honored the header value (recorded via a patched _sleep).
    slept: list[float] = []
    real_sleep = cbase._sleep
    cbase._sleep = lambda s: slept.append(s)
    try:
        state = {"n": 0}

        def retry_then_ok(req):
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "rate"})
            return httpx.Response(
                200, json={"records": [{"id": "r1", "fields": {"Body (EN)": "Hi"}}]}
            )

        at_bo = AirtableConnector(
            cfg, {"api_key": "patTEST"}, transport=httpx.MockTransport(retry_then_ok)
        )
        ok_bo, _m = at_bo.test()
        assert ok_bo, _m
        assert state["n"] == 2, ("should retry once after 429", state)
        assert slept == [7.0], ("must honor numeric Retry-After", slept)

        # A plain 400 is terminal — no retry, no sleep.
        slept.clear()
        state400 = {"n": 0}

        def always_400(req):
            state400["n"] += 1
            return httpx.Response(400, json={"error": "bad"})

        at_400 = AirtableConnector(
            cfg, {"api_key": "patTEST"}, transport=httpx.MockTransport(always_400)
        )
        ok_400, _m4 = at_400.test()
        assert not ok_400, "400 should surface as failure"
        assert state400["n"] == 1 and slept == [], ("400 must not retry", state400, slept)
    finally:
        cbase._sleep = real_sleep
    print("  backoff: 429+Retry-After retried (honored 7s); 400 terminal (no retry)")

    # ---- (a2) idempotency-aware retries: non-idempotent POST writes ----
    # A comment POST is non-idempotent (the write may land server-side before a
    # 5xx/timeout surfaces), so it must be attempted EXACTLY ONCE on 5xx and on a
    # transport error — never resent — and the caller records a single failure.
    slept.clear()
    cbase._sleep = lambda s: slept.append(s)
    try:
        jconn_id = cstore.create(
            "jira",
            "Idem jira",
            {
                "base_url": "https://team.atlassian.net",
                "jql": "project = LOC",
                "source_field": "summary",
                "target_locales": "es-MX",
                "source_locale": "en-US",
            },
            {"email": "b@x.com", "api_token": "tok"},
            actor="admin",
        ).id

        # (i) a 500 on the comment POST -> ONE attempt, no retry, failure captured
        n500 = {"post": 0}

        def jira_post_500(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [{"key": "LOC-1", "fields": {"summary": "Hello"}}],
                    },
                )
            if path.endswith("/comment") and req.method == "POST":
                n500["post"] += 1
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(404, json={"path": path})

        ok5, _m5, sum5 = cstore.jira_translate(
            jconn_id, actor="auto", transport=httpx.MockTransport(jira_post_500)
        )
        assert n500["post"] == 1, ("non-idempotent 500 POST must NOT retry", n500)
        assert not ok5 and sum5["failures"], ("500 write -> failure captured", sum5)
        assert slept == [], ("no backoff sleep for a non-retried write", slept)

        # (ii) a ReadTimeout on the comment POST -> ONE attempt, failure captured
        slept.clear()
        n_to = {"post": 0}

        def jira_post_timeout(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [{"key": "LOC-1", "fields": {"summary": "Hello"}}],
                    },
                )
            if path.endswith("/comment") and req.method == "POST":
                n_to["post"] += 1
                raise httpx.ReadTimeout("timed out", request=req)
            return httpx.Response(404, json={"path": path})

        okt, _mt, sumt = cstore.jira_translate(
            jconn_id, actor="auto", transport=httpx.MockTransport(jira_post_timeout)
        )
        assert n_to["post"] == 1, ("non-idempotent POST timeout must NOT retry", n_to)
        assert not okt and sumt["failures"], ("timed-out write -> failure captured", sumt)
        assert slept == [], ("no backoff sleep for a non-retried timeout", slept)

        # (iii) an idempotent GET still retries on 5xx (the search endpoint)
        slept.clear()
        g500 = {"n": 0}

        def jira_get_500_once(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                g500["n"] += 1
                if g500["n"] == 1:
                    return httpx.Response(500, json={"error": "flaky"})
                # distinct source text per sub-test so the changelog hash-gate
                # (per issue,locale source hash) doesn't suppress this write —
                # these sub-tests exercise transport retry, not the gate.
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [{"key": "LOC-1", "fields": {"summary": "Hello GET"}}],
                    },
                )
            if path.endswith("/comment") and req.method == "POST":
                return httpx.Response(201, json={"id": "1"})
            return httpx.Response(404, json={"path": path})

        okg, _mg, sumg = cstore.jira_translate(
            jconn_id, actor="auto", transport=httpx.MockTransport(jira_get_500_once)
        )
        assert g500["n"] == 2, ("idempotent GET must retry the 500", g500)
        assert okg and sumg["written"] == 1, ("GET recovered, comment posted", sumg)
        assert slept, ("idempotent retry slept once", slept)

        # (iv) a 429 on the comment POST IS retried (server refused) and succeeds
        slept.clear()
        n429 = {"post": 0}

        def jira_post_429_then_ok(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                # distinct source text (see above) so the hash-gate doesn't
                # suppress — this sub-test exercises the 429 POST retry path.
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [{"key": "LOC-1", "fields": {"summary": "Hello 429"}}],
                    },
                )
            if path.endswith("/comment") and req.method == "POST":
                n429["post"] += 1
                if n429["post"] == 1:
                    return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": "rate"})
                return httpx.Response(201, json={"id": "1"})
            return httpx.Response(404, json={"path": path})

        ok429, _m429, sum429 = cstore.jira_translate(
            jconn_id, actor="auto", transport=httpx.MockTransport(jira_post_429_then_ok)
        )
        assert n429["post"] == 2, ("429 on a POST write IS retried", n429)
        assert ok429 and sum429["written"] == 1 and not sum429["failures"], sum429
        assert slept == [3.0], ("Retry-After honored on the retried POST", slept)
    finally:
        cbase._sleep = real_sleep
    print("  idempotency: POST write ONE attempt on 5xx/timeout; GET retries; 429 POST retried+ok")

    # ---- (b) partial-failure capture ----
    # patch_records: batch 2 (records 10-19) fails mid-run; every batch is still
    # attempted and the failed record ids come back.
    def flaky_patch_transport():
        def handler(req):
            path = req.url.path
            if req.method == "GET" and path == "/v0/meta/bases/appPF/tables":
                return httpx.Response(200, json={"tables": []})
            if req.method == "PATCH":
                body = json.loads(req.content)
                ids = [r["id"] for r in body["records"]]
                if "rec10" in ids:  # the second batch
                    return httpx.Response(422, json={"error": "invalid"})
                return httpx.Response(200, json={"records": body["records"]})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    at_pf = AirtableConnector(
        {"base_id": "appPF", "table": "T"}, {"api_key": "patPF"}, transport=flaky_patch_transport()
    )
    updates = {f"rec{i}": {"F": f"v{i}"} for i in range(25)}  # 3 batches: 0-9, 10-19, 20-24
    written, failed_ids, pf_msg = at_pf.patch_records("appPF", "T", updates)
    assert written == 15, ("batches 1 and 3 should still write", written, pf_msg)
    assert set(failed_ids) == {f"rec{i}" for i in range(10, 20)}, failed_ids
    print(
        f"  partial: patch_records wrote {written}, all batches attempted, {len(failed_ids)} failed ids captured"  # noqa: E501
    )

    # jira_translate: one issue's comment POST fails -> ok=True (some written),
    # written < total, and summary.failures carries the issue_key + locale.
    def jira_one_fail_transport():
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [
                            {"key": "LOC-1", "fields": {"summary": "One"}},
                            {"key": "LOC-2", "fields": {"summary": "Two"}},
                        ],
                    },
                )
            if path.endswith("/comment") and req.method == "POST":
                if "/LOC-2/" in path:
                    return httpx.Response(400, json={"error": "bad issue"})
                return httpx.Response(201, json={"id": "1"})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    jpf = cstore.create(
        "jira",
        "Jira partial",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    ok_jp, msg_jp, sum_jp = cstore.jira_translate(
        jpf.id, actor="auto", transport=jira_one_fail_transport()
    )
    assert ok_jp, ("some comments landed -> ok=True", msg_jp)  # 2 issues x 1 locale
    assert sum_jp["written"] == 1 and sum_jp["issues"] == 2, sum_jp  # LOC-1 ok, LOC-2 failed
    assert len(sum_jp["failures"]) == 1, sum_jp["failures"]
    fjp = sum_jp["failures"][0]
    assert fjp["issue_key"] == "LOC-2" and fjp["locale"] == "es-MX", fjp
    print(f"  partial: jira_translate wrote {sum_jp['written']}/2, failures carry issue_key+locale")

    # ---- (c) FAILED run through the JOB engine keeps its structured failures ----
    # The job handler runs on a worker thread with transport=None, so we NEVER
    # let it reach a real API: stub translate_now BEFORE submitting.
    # A run that returns ok=False WITH a non-empty summary must land FAILED (that
    # is the correct semantics — a run where nothing succeeded is a failure) but
    # WITHOUT losing the structured failures: both job.error AND result.failures
    # are kept, so the UI can still offer a targeted retry off a FAILED job.
    tc = TestClient(app)
    real_tn = cstore.translate_now
    set_pieces(
        "app.services.connectors.store",
        "translate_now",
        lambda cid, actor="", transport=None: (
            False,
            "wrote 0 of 2; 2 failed",
            {
                "written": 0,
                "issues": 2,
                "failures": [
                    {"issue_key": "LOC-1", "locale": "es-MX", "error": "500"},
                    {"issue_key": "LOC-2", "locale": "es-MX", "error": "400"},
                ],
            },
        ),
    )
    try:
        jid_job = jobs.submit(
            "connector_translate", {"connection_id": jpf.id, "actor": "auto"}, actor="auto"
        )
        d_job = wait_job(tc, jid_job)
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real_tn)
    assert d_job["status"] == JobStatus.FAILED, ("ok=False run must be FAILED", d_job)
    assert "2 failed" in (d_job["error"] or ""), ("error must carry the message", d_job)
    assert d_job["result"]["failures"], ("failures must survive on the FAILED job result", d_job)
    assert d_job["result"]["ok"] is False and len(d_job["result"]["failures"]) == 2, d_job
    print("  job: ok=False run -> FAILED, but error AND result.failures both preserved")

    # ok=True WITH item failures (a partial-but-mostly-successful run) stays
    # COMPLETED and keeps result.failures so a targeted retry is still offered.
    set_pieces(
        "app.services.connectors.store",
        "translate_now",
        lambda cid, actor="", transport=None: (
            True,
            "wrote 1 of 2; 1 failed",
            {
                "written": 1,
                "issues": 2,
                "failures": [{"issue_key": "LOC-2", "locale": "es-MX", "error": "400"}],
            },
        ),
    )
    try:
        jid_ok = jobs.submit(
            "connector_translate", {"connection_id": jpf.id, "actor": "auto"}, actor="auto"
        )
        d_ok = wait_job(tc, jid_ok)
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real_tn)
    assert d_ok["status"] == JobStatus.COMPLETED, ("ok=True partial run stays COMPLETED", d_ok)
    assert d_ok["result"]["failures"] and d_ok["result"]["ok"] is True, d_ok
    print("  job: ok=True partial run -> COMPLETED, result.failures preserved")

    # ok=False with an EMPTY summary -> FAILED plain (no result payload), matching
    # the verify_jobs "failure lands in job.error" assertion (total failure).
    set_pieces(
        "app.services.connectors.store",
        "translate_now",
        lambda cid, actor="", transport=None: (False, "boom: API down", {}),
    )
    try:
        jid_tot = jobs.submit(
            "connector_translate", {"connection_id": jpf.id, "actor": "auto"}, actor="auto"
        )
        d_tot = wait_job(tc, jid_tot)
    finally:
        set_pieces("app.services.connectors.store", "translate_now", real_tn)
    assert d_tot["status"] == JobStatus.FAILED and "API down" in d_tot["error"], d_tot
    assert not d_tot["result"].get("failures"), ("empty-summary failure keeps no result", d_tot)
    print("  job: ok=False empty summary -> FAILED plain (job.error only)")

    # ---- (d) retry re-runs ONLY the failed subset ----
    retry_seen = {"keys": None}

    def real_jt(
        cid,
        issue_keys=None,
        jql=None,
        target_locales=None,
        actor="",
        transport=None,
        only_pairs=None,
    ):
        retry_seen["keys"] = issue_keys
        retry_seen["locales"] = target_locales
        retry_seen["pairs"] = only_pairs
        return True, "retried", {"written": 1, "issues": 1, "failures": []}

    saved_jt = cstore.jira_translate
    set_pieces("app.services.connectors.store", "jira_translate", real_jt)
    try:
        okr, _msgr, _sumr = cstore.retry_failed(
            jpf.id, [{"issue_key": "LOC-2", "locale": "es-MX", "error": "400"}], actor="auto"
        )
    finally:
        set_pieces("app.services.connectors.store", "jira_translate", saved_jt)
    assert okr and retry_seen["keys"] == ["LOC-2"], (
        "retry must narrow to failed key only",
        retry_seen,
    )
    assert retry_seen["locales"] == ["es-MX"], retry_seen
    assert retry_seen["pairs"] == {("LOC-2", "es-MX")}, ("exact pair passed", retry_seen)
    print("  retry: store.retry_failed re-runs ONLY the failed subset (issue_keys=[LOC-2])")

    # retry via the transport request log (real jira_translate, only LOC-2 touched)
    def jira_retry_transport(searched: list, posted: list):
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                searched.append(dict(req.url.params))
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [{"key": "LOC-2", "fields": {"summary": "Two"}}],
                    },
                )
            if path.endswith("/comment") and req.method == "POST":
                posted.append(path)
                return httpx.Response(201, json={"id": "1"})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    searched, posted_r = [], []
    okr2, _m, sumr2 = cstore.retry_failed(
        jpf.id,
        [{"issue_key": "LOC-2", "locale": "es-MX", "error": "400"}],
        actor="auto",
        transport=jira_retry_transport(searched, posted_r),
    )
    assert okr2, sumr2
    assert searched and "LOC-2" in searched[0].get("jql", ""), ("JQL must scope to LOC-2", searched)
    assert all("/LOC-2/" in p for p in posted_r) and posted_r, ("only LOC-2 written", posted_r)
    print(f"  retry: transport log shows only LOC-2 fetched/written ({len(posted_r)} comment)")

    # ---- FIX 2: exact-pair retry — 2 keys x 2 locales, only the failed pairs written ----
    # A prior run of 2 issues x 2 locales fails on exactly 2 of the 4 pairs. The
    # retry fetches both keys (JQL scopes to the failed keys) but the WRITE loop
    # must post ONLY the 2 failed (key, locale) pairs — never a pair that already
    # succeeded (which would double-post a comment). Asserted via the POST log.
    jpair = cstore.create(
        "jira",
        "Jira pair retry",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary",
            "target_locales": "es-MX, de-DE",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )

    def jira_pair_transport(posts: list):
        """Records (issue_key, locale) actually posted, parsed from the ADF body."""

        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [
                            {"key": "LOC-1", "fields": {"summary": "One"}},
                            {"key": "LOC-2", "fields": {"summary": "Two"}},
                        ],
                    },
                )
            if path.endswith("/comment") and req.method == "POST":
                key = path.split("/issue/")[1].split("/")[0]
                body = json.loads(req.content)["body"]
                # the label locale is the first line of the first paragraph
                first = body["content"][0]["content"][0]["text"]
                loc = "es-MX" if "es-MX" in first else ("de-DE" if "de-DE" in first else "?")
                posts.append((key, loc))
                return httpx.Response(201, json={"id": "1"})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    # failed pairs: LOC-1/de-DE and LOC-2/es-MX (the other two already succeeded)
    jp_failures = [
        {"issue_key": "LOC-1", "locale": "de-DE", "error": "500"},
        {"issue_key": "LOC-2", "locale": "es-MX", "error": "400"},
    ]
    jp_posts = []
    okp, _mp, sump = cstore.retry_failed(
        jpair.id, jp_failures, actor="auto", transport=jira_pair_transport(jp_posts)
    )
    assert okp, sump
    assert set(jp_posts) == {("LOC-1", "de-DE"), ("LOC-2", "es-MX")}, (
        "only failed pairs written",
        jp_posts,
    )
    assert len(jp_posts) == 2, ("no double-post of succeeded pairs", jp_posts)
    print(
        "  pairs (jira): retry wrote ONLY the 2 failed (key, locale) pairs, not the 2x2 cross product"  # noqa: E501
    )

    # Confluence: 2 pages x 2 locales, retry only the 2 failed (page, locale)
    # pairs. The create-page POST log must show exactly those two children.
    cpair = cstore.create(
        "confluence",
        "Conf pair retry",
        {
            "base_url": "https://team.atlassian.net",
            "space_key": "HELP",
            "target_locales": "es-MX, de-DE",
            "source_locale": "en-US",
            "title_pattern": "[{loc}] {title}",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )

    cpt = FakeConfluence()
    cpt.add_space("HELP", "Help")
    cpt.add_page("HELP", "Alpha", "<p>Alpha body</p>", pid="100")
    cpt.add_page("HELP", "Beta", "<p>Beta body</p>", pid="101")

    # failed pairs: page 100/de-DE and page 101/es-MX
    cp_failures = [
        {"page_id": "100", "locale": "de-DE", "error": "500"},
        {"page_id": "101", "locale": "es-MX", "error": "400"},
    ]
    okc, _mc, sumc = cstore.retry_failed(
        cpair.id, cp_failures, actor="auto", transport=cpt.transport()
    )
    assert okc, sumc
    cp_creates = [e["body"]["title"] for e in cpt.writes("POST")]
    # exactly the 2 failed (page, locale) pairs created — Alpha only in de-DE,
    # Beta only in es-MX; never the 2x2 cross product (mock MT may prefix the
    # translated title, so match on locale + page word, not the exact string).
    assert len(cp_creates) == 2, ("no duplicate creation of succeeded pairs", cp_creates)
    assert any(t.startswith("[de-DE]") and "Alpha" in t for t in cp_creates), cp_creates
    assert any(t.startswith("[es-MX]") and "Beta" in t for t in cp_creates), cp_creates
    assert not any("[es-MX]" in t and "Alpha" in t for t in cp_creates), (
        "Alpha/es-MX already ok",
        cp_creates,
    )
    assert not any("[de-DE]" in t and "Beta" in t for t in cp_creates), (
        "Beta/de-DE already ok",
        cp_creates,
    )
    print("  pairs (confluence): retry created ONLY the 2 failed (page, locale) child pages")

    # ---- routes + inbound API ----
    c = TestClient(app)
    idx = c.get("/connectors").text
    assert "Airtable" in idx and "Jira" in idx and "Confluence" in idx, "connector tiles missing"
    typ = c.get("/connectors/jira").text
    assert "API token" in typ and "Create" in typ, "type page missing form"

    r = c.post(
        "/connectors/airtable",
        data={
            "name": "Routed",
            "base_id": "appR",
            "table": "T",
            "source_field": "S",
            "target_field": "D",
            "api_key": "patROUTE",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/connectors/connection/" in r.headers["location"]
    cid = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        assert "patROUTE" not in s.get(Connection, cid).secret_enc
    print("  routes: index + type page render, create encrypts secret")

    # ---- field-mapping picker: browse routes (all POST + _ai_block + JSON) ----
    # These are token-spending live-network reads via the store, so route tests
    # stub the store functions (like the suite stubs translate_now/retry_failed)
    # to stay hermetic; the store funcs themselves are unit-tested above with a
    # MockTransport. Gate matches sibling _ai_block routes: auth is off by default
    # here (as for the create POST just above), so the gate passes -> 200/JSON.
    saved_containers = cstore.containers
    saved_sf = cstore.schema_fields
    saved_bcu = cstore.browse_containers_unsaved
    set_pieces(
        "app.services.connectors.store",
        "containers",
        lambda cid_, transport=None: [
            {"id": "appX", "name": "Marketing", "tables": [{"id": "t1", "name": "Content"}]}
        ],
    )
    set_pieces(
        "app.services.connectors.store",
        "schema_fields",
        lambda cid_, base, table, transport=None: ["Notes", "Notes (es-MX)"],
    )
    set_pieces(
        "app.services.connectors.store",
        "browse_containers_unsaved",
        lambda kind, cfg, secret, transport=None: [{"id": "appY", "name": "Sales", "tables": []}],
    )
    try:
        # containers: success shape
        bc = c.post(f"/connectors/connection/{cid}/browse/containers")
        assert bc.status_code == 200 and bc.json()["ok"] is True, bc.text
        assert bc.json()["containers"][0]["id"] == "appX", bc.text
        # containers: unknown cid -> 404 + ok:False
        bc404 = c.post("/connectors/connection/99999999/browse/containers")
        assert bc404.status_code == 404 and bc404.json()["ok"] is False, bc404.text
        # fields: success shape (Airtable connection)
        bf = c.post(
            f"/connectors/connection/{cid}/browse/fields", data={"base": "appX", "table": "Content"}
        )
        assert bf.status_code == 200 and bf.json()["fields"] == ["Notes", "Notes (es-MX)"], bf.text
        # fields: unknown cid -> 404
        bf404 = c.post(
            "/connectors/connection/99999999/browse/fields",
            data={"base": "appX", "table": "Content"},
        )
        assert bf404.status_code == 404, bf404.text
        # fields: ConnectorError -> 400 with the message
        set_pieces(
            "app.services.connectors.store",
            "schema_fields",
            lambda cid_, base, table, transport=None: (_ for _ in ()).throw(
                cbase.ConnectorError("Table 'Nope' not found.")
            ),
        )
        bferr = c.post(
            f"/connectors/connection/{cid}/browse/fields", data={"base": "appX", "table": "Nope"}
        )
        assert bferr.status_code == 400 and "not found" in bferr.json()["error"], bferr.text
        set_pieces(
            "app.services.connectors.store",
            "schema_fields",
            lambda cid_, base, table, transport=None: ["Notes"],
        )
        # fields: non-airtable connection -> 400 (Airtable-only)
        jrow = cstore.create(
            "jira",
            "Jira browse",
            {"base_url": "https://team.atlassian.net"},
            {"email": "b@x.com", "api_token": "t"},
            actor="admin",
        )
        bfj = c.post(
            f"/connectors/connection/{jrow.id}/browse/fields", data={"base": "x", "table": "y"}
        )
        assert bfj.status_code == 400 and "Airtable" in bfj.json()["error"], bfj.text
        # unsaved browse: success shape
        bu = c.post(
            "/connectors/jira/browse",
            data={"base_url": "https://team.atlassian.net", "email": "b@x.com", "api_token": "t"},
        )
        assert bu.status_code == 200 and bu.json()["containers"][0]["id"] == "appY", bu.text
        # unsaved browse: unknown kind -> 404
        bu404 = c.post("/connectors/notakind/browse", data={"api_key": "x"})
        assert bu404.status_code == 404 and bu404.json()["ok"] is False, bu404.text
        print(
            "  browse routes: containers/fields/unsaved 200 shapes; 404 cid/kind; 400 non-airtable + ConnectorError"  # noqa: E501
        )
    finally:
        set_pieces("app.services.connectors.store", "containers", saved_containers)
        set_pieces("app.services.connectors.store", "schema_fields", saved_sf)
        set_pieces("app.services.connectors.store", "browse_containers_unsaved", saved_bcu)

    # ---- FIX 3: browse routes are gated by _ai_block (auth ON -> 403) ----
    # With the login wall ON, a signed-in but NON-privileged caller (a translator:
    # can_write True so the middleware lets the mutating POST through, but
    # can_tune_ai False so _ai_block refuses it) must get 403 on ALL THREE browse
    # routes. This fails loudly if _ai_block is ever dropped from a browse route —
    # the middleware alone would let a translator's write reach the handler.
    # An anonymous caller is refused earlier (middleware redirect), asserted too.
    from app.models import Role as _Role  # local import; suite only needs it here

    settings.set_many({"security_auth_required": "1"})
    try:
        # need user_count()>0 so the middleware doesn't redirect to /auth/setup
        security.create_user("gate-admin@x.com", "Gate Admin", _Role.ADMIN, "gate-admin-pass-12")
        security.create_user("gate-tx@x.com", "Gate Tx", _Role.TRANSLATOR, "gate-tx-pass-1234")
        tx = TestClient(app)
        csrf_for(tx)  # auth on: login POST needs the token
        lr = tx.post(
            "/auth/login",
            data={"email": "gate-tx@x.com", "password": "gate-tx-pass-1234", "next": "/"},
            follow_redirects=False,
        )
        assert lr.status_code == 303, ("translator login should succeed", lr.status_code)
        csrf_for(tx)  # login rotated the session -> refresh so the browse POSTs carry a valid token
        for path in (
            f"/connectors/connection/{cid}/browse/containers",
            f"/connectors/connection/{cid}/browse/fields",
            "/connectors/airtable/browse",
        ):
            gr = tx.post(path, data={"base": "x", "table": "y"}, follow_redirects=False)
            assert gr.status_code == 403, (
                f"_ai_block must 403 a translator: {path} -> {gr.status_code}"
            )
            # anonymous is refused before the handler too (middleware redirect)
            anon = TestClient(app).post(
                path, data={"base": "x", "table": "y"}, follow_redirects=False
            )
            assert anon.status_code in (303, 401, 403), (
                f"anon must be refused: {path} -> {anon.status_code}"
            )
    finally:
        settings.set_many({"security_auth_required": "0"})  # restore single-operator mode
    print(
        "  browse gate: all 3 browse routes 403 for a translator when auth ON (_ai_block enforced)"
    )

    # ---- picker UI hooks render + connectors.js is served ----
    add_page = c.get("/connectors/airtable").text
    assert 'data-browse="add"' in add_page and 'data-kind="airtable"' in add_page, (
        "add form missing browse hooks"
    )
    assert "data-connector-browse" in add_page and "/static/connectors.js" in add_page, (
        "add form missing picker JS"
    )
    conn_page = c.get(f"/connectors/connection/{cid}").text
    assert 'data-browse="settings"' in conn_page and f'cid="{cid}"' in conn_page, (
        "settings form missing browse hooks"
    )
    assert "data-connector-browse" in conn_page and "/static/connectors.js" in conn_page, (
        "settings form missing picker JS"
    )
    assert c.get("/static/connectors.js").status_code == 200, "connectors.js must be served"
    # connectors.js parses (node --check if node is on PATH; otherwise skip cleanly)
    import shutil
    import subprocess

    node = shutil.which("node")
    js_path = os.path.join(_ROOT, "app", "static", "connectors.js")
    if node:
        rc = subprocess.run([node, "--check", js_path], capture_output=True, text=True)
        assert rc.returncode == 0, ("connectors.js failed node --check", rc.stderr)
        print(
            "  picker UI: forms carry data-browse hooks; connectors.js served + parses (node --check)"  # noqa: E501
        )
    else:
        print(
            "  picker UI: forms carry data-browse hooks; connectors.js served (node --check skipped)"  # noqa: E501
        )

    token = connector_api_token()
    # F2 (audit 2026-09-05): the liveness probe stays open but returns ONLY {ok, version} to an
    # anonymous caller -- the enabled-connector-kind enumeration moved behind the docs-scoped key
    # (it used to leak the install's connector topology to anyone who could reach the port).
    from app.services import apikeys as _apikeys

    _h = c.get("/api/connectors/health")
    assert _h.status_code == 200, _h.status_code
    _hb = _h.json()
    assert set(_hb.keys()) == {"ok", "version"}, ("anon health must be {ok, version} only", _hb)
    assert "connectors" not in _hb, ("anon health must NOT enumerate connector kinds", _hb)
    # L4 (audit 2026-09-05 round-1): the anonymous version is COARSENED to MAJOR.MINOR so the one
    # route that answers without a key is not a free known-CVE fingerprint. Pin the coarse value and
    # prove the exact patch release never leaks anonymously.
    from app.web.api import _APP_VERSION as _AV
    from app.web.api import _APP_VERSION_COARSE as _AVC

    assert _hb["version"] == _AVC, ("anon version must be the coarse major.minor", _hb, _AVC)
    if _AV.count(".") >= 2 and all(p.isdigit() for p in _AV.split(".")[:2]):
        assert _hb["version"] != _AV, ("anon version must be coarsened, not the exact release", _hb)
    # LOW-3 (audit 2026-09-05 round-2): a git-tag-shaped release with a leading 'v' must ALSO
    # coarsen ('v0.8.1' -> '0.8'), not fall through the numeric gate and leak the full patch.
    # P2a round 2: the coarsener now lives on app.release (the ONE seam both keyless routes
    # -- /api/connectors/health here and /health -- share); app.web.api re-exports nothing.
    from app.release import coarse_version as _cv

    assert _cv("v0.8.1") == "0.8", ("a leading 'v' must coarsen", _cv("v0.8.1"))
    assert _cv("0.8.1") == "0.8" and _cv("unknown") == "unknown" and _cv("") == "unknown"
    # ...but a docs-scoped user key DOES get the enabled kinds (the enumeration, now gated).
    _hu = security.create_user("health-f2@x.com", "H", "admin", "health-f2-pw-123456")
    _hk_row, _hk = _apikeys.create_key(_hu.id, "health-f2-docs", scopes="api docs")
    _dh = c.get("/api/connectors/health", headers={"X-Loc-Token": _hk}).json()
    assert "connectors" in _dh and isinstance(_dh["connectors"], list), (
        "a docs-scoped key must see the enabled connector kinds",
        _dh,
    )
    assert _dh["version"] == _hb["version"], "the version must match across auth states"
    # the shared connector (lsk_) token is NOT a docs-scoped USER key, so it stays on the bare
    # probe (no kind enumeration).
    _ct = c.get("/api/connectors/health", headers={"X-Loc-Token": token}).json()
    assert "connectors" not in _ct, ("the shared connector token must not enumerate kinds", _ct)
    print(
        "  F2: /api/connectors/health is {ok, version} anon; enabled kinds only behind a "
        "docs-scoped user key"
    )
    assert c.get("/api/connectors").status_code == 401, "must require token"
    assert c.get("/api/connectors", headers={"X-Loc-Token": token}).status_code == 403
    from tests._api_fixture import user_api_token

    token = user_api_token()
    assert c.get("/api/connectors", headers={"X-Loc-Token": token}).status_code == 200
    assert c.post(f"/api/connectors/{cid}/pull").status_code == 401, "pull must require token"
    assert c.post(f"/api/connectors/{cid}/sync").status_code == 401, "sync must require token"
    assert c.post(f"/api/connectors/{cid}/airtable/webhook").status_code == 401, (
        "webhook must require token"
    )
    assert c.post(f"/api/connectors/{cid}/jira/translate").status_code == 401, (
        "jira translate must require token"
    )
    assert c.post(f"/api/connectors/{cid}/confluence/translate").status_code == 401, (
        "confluence translate must require token"
    )
    assert c.post(f"/api/connectors/{cid}/retry").status_code == 401, "retry must require token"
    print(
        "  inbound API: health open; pull/sync/webhook/jira/confluence/retry gated by X-Loc-Token"
    )

    # ---- (d) retry routes: API (token) + web (session) enqueue ----
    settings.set_many({"security_auth_required": "0"})  # single-operator mode from here on

    # Enqueued retry jobs run on a worker with transport=None — keep them offline
    # by stubbing retry_failed so no worker can reach a real API. We only assert
    # the Job ROW the routes create (kind + payload); the worker outcome is moot.
    saved_rf = cstore.retry_failed
    set_pieces(
        "app.services.connectors.store",
        "retry_failed",
        lambda cid, failures, actor="", transport=None: (
            True,
            "stub",
            {"failures": []},
        ),
    )

    def _seed_job(conn_id, result=None, kind="connector_translate", status=JobStatus.COMPLETED):
        """A prior job to retry off of — carries connection_id in its payload so
        the cross-connection guard accepts (or, for another id, rejects) it."""
        with Session(engine) as s:
            j = Job(
                kind=kind,
                status=status,
                created_by="",
                payload={"connection_id": conn_id},
                result=result or {},
            )
            s.add(j)
            s.commit()
            s.refresh(j)
            return j.id

    try:
        # API retry with an explicit failures body -> 202 + a connector_translate job
        drain_jobs()  # a leftover active job would make submit_unique dedupe
        fails_body = {"failures": [{"issue_key": "LOC-2", "locale": "es-MX", "error": "400"}]}
        rr = c.post(
            f"/api/connectors/{jpf.id}/retry", headers={"X-Loc-Token": token}, json=fails_body
        )
        assert rr.status_code == 202, (rr.status_code, rr.text)
        assert rr.json().get("existing") is not True, ("must be a FRESH job", rr.text)
        api_jid = rr.json()["job_id"]
        with Session(engine) as s:
            jrow = s.get(Job, api_jid)
        assert jrow.kind == "connector_translate", jrow.kind
        assert jrow.payload.get("retry_failures") == fails_body["failures"], jrow.payload
        assert jrow.payload.get("connection_id") == jpf.id, jrow.payload
        wait_job(c, api_jid)  # let it reach terminal state — never leak an active job

        # API retry with no failures / unknown job -> 400
        assert (
            c.post(
                f"/api/connectors/{jpf.id}/retry",
                headers={"X-Loc-Token": token},
                json={"job_id": 999999},
            ).status_code
            == 400
        )

        # Web retry: reads failures from a stored job result via job_id form field
        drain_jobs()
        seed_jid = _seed_job(
            jpf.id,
            result={
                "message": "partial",
                "ok": False,
                "failures": [{"issue_key": "LOC-2", "locale": "es-MX", "error": "400"}],
            },
        )
        wr = c.post(
            f"/connectors/connection/{jpf.id}/retry",
            data={"job_id": str(seed_jid)},
            follow_redirects=False,
        )
        assert wr.status_code == 303 and "job=" in wr.headers["location"], wr.headers
        assert "already%20running" not in wr.headers["location"].replace("+", "%20"), (
            "must be a FRESH job, not a dedup hit",
            wr.headers["location"],
        )
        web_jid = loc_job_id(wr.headers["location"])
        with Session(engine) as s:
            wjob = s.get(Job, web_jid)
        assert wjob.payload.get("retry_failures"), wjob.payload
        wait_job(c, web_jid)  # drain before the next section

        # Web retry with a job that has no failures -> redirect with an error message
        empty_jid = _seed_job(jpf.id, result={})
        wr2 = c.post(
            f"/connectors/connection/{jpf.id}/retry",
            data={"job_id": str(empty_jid)},
            follow_redirects=False,
        )
        assert (
            wr2.status_code == 303
            and "ok=0" in wr2.headers["location"]
            and "No%20failed" in wr2.headers["location"]
        ), wr2.headers
        print(
            "  retry routes: API 401->202 (token, failures body); web 303 enqueues from job result"
        )

        # ---- FIX 3: cross-connection + wrong-kind guard ----
        # A job that BELONGS to another connection must not seed a retry here.
        other = cstore.create(
            "jira",
            "Other conn",
            {"base_url": "https://team.atlassian.net", "jql": "project = X"},
            {"email": "b@x.com", "api_token": "tok"},
            actor="admin",
        )
        foreign_jid = _seed_job(
            other.id, result={"failures": [{"issue_key": "X-1", "locale": "es-MX", "error": "400"}]}
        )
        # web: 303 back to THIS connection with an error message, no job started
        wf = c.post(
            f"/connectors/connection/{jpf.id}/retry",
            data={"job_id": str(foreign_jid)},
            follow_redirects=False,
        )
        assert (
            wf.status_code == 303
            and "ok=0" in wf.headers["location"]
            and "does%20not%20belong" in wf.headers["location"].lower().replace("+", "%20")
        ), wf.headers
        # API: 400 for the same foreign job
        af = c.post(
            f"/api/connectors/{jpf.id}/retry",
            headers={"X-Loc-Token": token},
            json={"job_id": foreign_jid},
        )
        assert af.status_code == 400 and not af.json().get("ok"), (af.status_code, af.text)
        # wrong-kind job (belongs to this connection but isn't connector_translate)
        wrongkind = _seed_job(
            jpf.id,
            kind="translate_document",
            result={"failures": [{"issue_key": "LOC-2", "locale": "es-MX", "error": "x"}]},
        )
        aw = c.post(
            f"/api/connectors/{jpf.id}/retry",
            headers={"X-Loc-Token": token},
            json={"job_id": wrongkind},
        )
        assert aw.status_code == 400, (aw.status_code, aw.text)
        ww = c.post(
            f"/connectors/connection/{jpf.id}/retry",
            data={"job_id": str(wrongkind)},
            follow_redirects=False,
        )
        assert ww.status_code == 303 and "does%20not%20belong" in ww.headers[
            "location"
        ].lower().replace("+", "%20"), ww.headers
        print("  retry guard: cross-connection job -> web 303 error, API 400; wrong-kind rejected")

        # ---- FIX 5: malformed / oversized failures are rejected or scrubbed ----
        # not-a-list -> 400, nothing enqueued
        n_jobs_before = len(jobs.recent(limit=500))
        bad1 = c.post(
            f"/api/connectors/{jpf.id}/retry",
            headers={"X-Loc-Token": token},
            json={"failures": "not a list"},
        )
        assert bad1.status_code == 400, bad1.text
        # list of entries with no usable id -> empty valid set -> 400
        bad2 = c.post(
            f"/api/connectors/{jpf.id}/retry",
            headers={"X-Loc-Token": token},
            json={"failures": [{"locale": "es-MX"}, "junk", 42, {"error": "x"}]},
        )
        assert bad2.status_code == 400, bad2.text
        assert len(jobs.recent(limit=500)) == n_jobs_before, "malformed retry must not enqueue"
        # mixed: one good + oversized/junk entries -> good kept & capped, junk dropped
        drain_jobs()  # must create a FRESH job, not dedupe onto a leftover
        big = "Z" * 5000
        mixed = c.post(
            f"/api/connectors/{jpf.id}/retry",
            headers={"X-Loc-Token": token},
            json={
                "failures": [
                    {"issue_key": "LOC-2", "locale": "es-MX", "error": big, "bogus": "drop me"},
                    {"nope": "no id here"},
                    "garbage",
                ]
            },
        )
        assert mixed.status_code == 202, mixed.text
        assert mixed.json().get("existing") is not True, ("must be a FRESH job", mixed.text)
        with Session(engine) as s:
            mrow = s.get(Job, mixed.json()["job_id"])
        kept = mrow.payload["retry_failures"]
        assert len(kept) == 1 and kept[0]["issue_key"] == "LOC-2", kept
        assert "bogus" not in kept[0] and len(kept[0]["error"]) <= 500, kept
        wait_job(c, mixed.json()["job_id"])  # drain before the next section
        # entry-COUNT cap (F2): the per-value cap bounds each field, not the list length;
        # the only_pairs bypass reaches all flow kinds, so an oversized list must be rejected
        # with a DISTINCT 400 naming the cap (not the generic "no failed items"), no enqueue.
        n_jobs_before_cap = len(jobs.recent(limit=5000))
        toomany = c.post(
            f"/api/connectors/{jpf.id}/retry",
            headers={"X-Loc-Token": token},
            json={"failures": [{"issue_key": f"LOC-{i}", "locale": "es-MX"} for i in range(1001)]},
        )
        assert toomany.status_code == 400, toomany.text
        assert "max 1000" in (toomany.json().get("error") or ""), (
            "the 400 must name the entry-count cap",
            toomany.text,
        )
        assert len(jobs.recent(limit=5000)) == n_jobs_before_cap, "over-cap retry must not enqueue"
        print(
            "  retry validation: non-list/no-id -> 400 (nothing enqueued); oversized capped, "
            "junk dropped; >1000-entry list -> 400 naming the cap"
        )

        # ---- FIX 6: dedup messaging when a sync is already in flight ----
        # Use a FRESH connection (no prior jobs contend) and hold its sync in a
        # blocking stubbed handler so submit_unique dedups the retry against it.
        import threading as _th

        ddc = cstore.create(
            "jira",
            "Dedup conn",
            {"base_url": "https://team.atlassian.net", "jql": "project = D"},
            {"email": "b@x.com", "api_token": "tok"},
            actor="admin",
        )
        release = _th.Event()
        real_tn2 = cstore.translate_now

        def _held_tn(cid, actor="", transport=None):
            release.wait(10)
            return True, "held", {"failures": []}

        set_pieces("app.services.connectors.store", "translate_now", _held_tn)
        live_jid = None
        try:
            drain_jobs()  # nothing else may be active or the seed sync dedupes
            live_jid, live_created = jobs.submit_unique(
                "connector_translate", {"connection_id": ddc.id, "actor": "auto"}, actor="auto"
            )
            assert live_created, "seed sync should be freshly created"
            # give a worker a moment to pick it up so it is RUNNING (still held)
            time.sleep(0.2)
            # API retry -> dedups, returns the LIVE job id + existing=true
            dd = c.post(
                f"/api/connectors/{ddc.id}/retry",
                headers={"X-Loc-Token": token},
                json={"failures": [{"issue_key": "D-1", "locale": "es-MX", "error": "400"}]},
            )
            assert dd.status_code == 202 and dd.json().get("existing") is True, dd.text
            assert dd.json()["job_id"] == live_jid, (dd.json(), live_jid)
            # web retry -> Location has the already-running msg + the live job id
            seed2 = _seed_job(
                ddc.id,
                result={"failures": [{"issue_key": "D-1", "locale": "es-MX", "error": "400"}]},
            )
            wd = c.post(
                f"/connectors/connection/{ddc.id}/retry",
                data={"job_id": str(seed2)},
                follow_redirects=False,
            )
            loc = wd.headers["location"]
            assert (
                wd.status_code == 303
                and loc_job_id(loc) == live_jid
                and "already%20running" in loc.replace("+", "%20")
            ), loc
        finally:
            release.set()
            # deterministic: wait for the held job to reach terminal state before
            # restoring the real translate_now (never a bare sleep)
            if live_jid:
                wait_job(c, live_jid)
            set_pieces("app.services.connectors.store", "translate_now", real_tn2)
        print("  retry dedup: in-flight sync -> API existing=true (live id); web msg + live job id")
    finally:
        # deterministic cleanup: no connector job may still be active when the
        # real retry_failed comes back (a worker could otherwise run it live)
        drain_jobs()
        set_pieces("app.services.connectors.store", "retry_failed", saved_rf)

    # ---- Chunk 1: credential rotation (test-before-swap) ----
    # (1) happy path: rotate an Airtable PAT -> new secret persisted, status
    # CONNECTED, and a "rotate" SyncLog row is written.
    rot = cstore.create(
        "airtable",
        "Rotate me",
        {"base_id": "appMARKETING", "table": "Content", "source_field": "Body (EN)"},
        {"api_key": "patOLD"},
        actor="admin",
    )
    ok_r, msg_r = cstore.rotate_secret(
        rot.id, {"api_key": "patNEW"}, actor="admin", transport=airtable_transport([])
    )
    assert ok_r, msg_r
    with Session(engine) as s:
        row_r = s.get(Connection, rot.id)
    assert cstore.dec_secret(row_r.secret_enc)["api_key"] == "patNEW", "new secret must persist"
    assert row_r.status == "connected", row_r.status
    rot_hist = cstore.history(rot.id)
    assert any(h.direction == "rotate" and h.ok for h in rot_hist), (
        "a successful rotate SyncLog row must exist",
        rot_hist,
    )
    print(
        "  rotate_secret: happy path -- patOLD -> patNEW persisted, CONNECTED, SyncLog rotate row"
    )

    # (2) failure: the new secret fails test() (401) -> (False, msg), OLD secret
    # is left completely untouched.
    def airtable_401_transport():
        def handler(req):
            return httpx.Response(401, json={"error": {"message": "Invalid authentication"}})

        return httpx.MockTransport(handler)

    ok_bad, _msg_bad = cstore.rotate_secret(
        rot.id, {"api_key": "patBOGUS"}, actor="admin", transport=airtable_401_transport()
    )
    assert not ok_bad, "a failing test() must not report success"
    with Session(engine) as s:
        row_bad = s.get(Connection, rot.id)
    assert cstore.dec_secret(row_bad.secret_enc)["api_key"] == "patNEW", (
        "a failed rotation must leave the previous secret untouched"
    )
    print("  rotate_secret: failed test (401) -> (False, msg), old secret (patNEW) intact")

    # (3) route: POST .../rotate redirects 303, with rotate_secret stubbed so no
    # real network call is possible from the route layer.
    saved_rotate = cstore.rotate_secret
    rotate_calls = []

    def _stub_rotate(cid_, new_secret, actor="", transport=None):
        rotate_calls.append((cid_, new_secret, actor))
        return True, "stubbed ok"

    set_pieces("app.services.connectors.store", "rotate_secret", _stub_rotate)
    try:
        rr_route = c.post(
            f"/connectors/connection/{rot.id}/rotate",
            data={"api_key": "patROUTED"},
            follow_redirects=False,
        )
        assert rr_route.status_code == 303, rr_route.status_code
        assert f"/connectors/connection/{rot.id}" in rr_route.headers["location"]
        assert "ok=1" in rr_route.headers["location"], rr_route.headers["location"]
        assert rotate_calls and rotate_calls[-1][1] == {"api_key": "patROUTED"}, rotate_calls
    finally:
        set_pieces("app.services.connectors.store", "rotate_secret", saved_rotate)
    print(
        "  route: POST /connectors/connection/{id}/rotate -> 303, calls rotate_secret with form secret"  # noqa: E501
    )

    # (4) auth-ON: a translator (write-capable but not AI-tune-capable) gets 403
    # from the rotate route, matching the existing _ai_block gate pattern.
    settings.set_many({"security_auth_required": "1"})
    try:
        tx2 = TestClient(app)
        csrf_for(tx2)
        lr2 = tx2.post(
            "/auth/login",
            data={"email": "gate-tx@x.com", "password": "gate-tx-pass-1234", "next": "/"},
            follow_redirects=False,
        )
        assert lr2.status_code == 303, ("translator login should succeed", lr2.status_code)
        csrf_for(tx2)
        gr2 = tx2.post(
            f"/connectors/connection/{rot.id}/rotate",
            data={"api_key": "patSHOULDNOTWORK"},
            follow_redirects=False,
        )
        assert gr2.status_code == 403, (
            f"_ai_block must 403 a translator on rotate: {gr2.status_code}"
        )
        anon2 = TestClient(app).post(
            f"/connectors/connection/{rot.id}/rotate", data={"api_key": "x"}, follow_redirects=False
        )
        assert anon2.status_code in (303, 401, 403), f"anon must be refused: {anon2.status_code}"
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  rotate gate: translator 403 when auth ON (_ai_block enforced); anon refused")

    # ---- HARDENING: a DEAD engine:<id> ref FAILS the sync LOUD (no silent mock) ----
    # An engine profile can be deleted/disabled AFTER a project's default_provider was
    # validly set to engine:<id>. The old behavior silently resolved to MockProvider
    # and wrote `[fr-FR] ...` placeholder output back to Airtable as a "success" — the
    # cardinal sin. The sync must now FAIL with a clear message naming the dead ref,
    # write NO mock output, and leave the connection in ERROR.
    from app.models import ConnStatus, Project
    from app.services import engines as _eng

    dead_conn = cstore.create(
        "airtable",
        "Dead-engine base",
        {
            "base_id": "appMARKETING",
            "table": "Content",
            "source_field": "Body (EN)",
            "target_field": "Body (FR)",
            "target_locale": "fr-FR",
        },
        {"api_key": "patDEAD"},
        actor="admin",
    )
    okp, _mp, dead_doc = cstore.pull_connection(
        dead_conn.id, actor="admin", transport=airtable_transport([])
    )
    assert okp and dead_doc, _mp
    prof = _eng.create_profile("DeadDeepL", "deepl", api_key="k1")
    _eng.update_profile(prof.id, enabled=False)  # disable AFTER it was validly referenced
    with Session(engine) as s:
        conn_row = s.get(Connection, dead_conn.id)
        proj = s.get(Project, conn_row.project_id)
        proj.default_provider = f"engine:{prof.id}"
        s.add(proj)
        # clear any target so a real translation would be attempted
        for sg in s.exec(select(Segment).where(Segment.document_id == dead_doc)).all():
            sg.target_text = ""
            s.add(sg)
        s.commit()
    dead_pushed = []
    ok_dead, msg_dead, _sum_dead = cstore.sync_connection(
        dead_conn.id, actor="auto", transport=airtable_transport(dead_pushed)
    )
    assert ok_dead is False, "sync with a dead engine ref must FAIL, not silently mock-translate"
    assert f"engine:{prof.id}" in msg_dead, f"failure must name the dead ref: {msg_dead!r}"
    assert not dead_pushed, "NO mock output may be written back on a dead-engine sync"
    with Session(engine) as s:
        assert s.get(Connection, dead_conn.id).status == ConnStatus.ERROR, "conn must be ERROR"
        # segments left untranslated (no `[fr-FR] ...` mock placeholder committed)
        for sg in s.exec(select(Segment).where(Segment.document_id == dead_doc)).all():
            assert not (sg.target_text or "").strip(), "no mock target may be committed"
    print(
        "  hardening: sync with a dead engine:<id> ref FAILS LOUD (names ref, "
        "ERROR status, no mock output written back)"
    )

    flash_redirect_encoding()

    print("CONNECTORS OK")


if __name__ == "__main__":
    main()
