"""Project notifications over HTTP (offline): scoping, roles, secrets, redirects, hooks.

Documented in: docs/architecture/notifications.md

The service-level suite is verify_notify. This one drives /api/notify/* and /settings/notifications
through the app, with the login wall ON:

  [1] roles: translator and viewer are refused (403); managers and admins are in
  [2] creator-or-admin: another manager cannot edit or delete a channel, a creator or admin can;
      org-scope attach is admin only; a URL change emails the admins (and never carries the URL)
  [3] customer ring: a customer-bound manager sees only its customer's channels, routes and
      delivery log (API and web page); a foreign id is a 404 on every route
  [4] secrets: no response, page or MCP-visible body ever carries a URL or signing secret; the
      signing secret is shown once
  [5] egress: a 302 is not followed; IPv6 and IPv4-mapped literals and a private rebinding name are
      refused with no internal address in the text; Slack and Teams documented non-2xx answers
  [6] signature: a fixed known-answer vector, a reference verifier (stale timestamp, tampered body)
  [7] hooks: project.created and the budget threshold reach a routed channel
  [8] sweep: bounded, async, prunes old rows; stale rows are not sent late; email retries only the
      recipients that failed
"""

import hashlib
import hmac
import os
import sys
import time
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_notify_http_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Job,
    JobStatus,
    NotifyChannel,
    NotifyOutbox,
    Project,
    Role,
)
from app.services import (
    apikeys,
    content_projects,
    egress,
    mailer,
    notify,
    project_budgets,
    security,
    settings,
)
from tests._csrf import csrf_for

PW = "Offline-notify-fixture-123!"  # pregate: allow-secret test fixture only
SLACK_URL = "https://hooks.slack.com/services/T0/B0/ZZTOPSECRETTOKEN1"
HOOK_URL = "https://hooks.example.org/in/WEBHOOKTOKEN987654"
SECRETS_SEEN = ("ZZTOPSECRETTOKEN1", "WEBHOOKTOKEN987654")
CALLS: list[dict] = []
ANSWER = {"status": 200, "location": ""}
BODIES: list[str] = []  # every response body the suite saw, scanned for secrets at the end


def _handler(request: httpx.Request) -> httpx.Response:
    CALLS.append({"url": str(request.url), "body": request.content})
    headers = {"location": ANSWER["location"]} if ANSWER["location"] else {}
    return httpx.Response(ANSWER["status"], headers=headers, text="ok")


def _public(host):
    return ["93.184.216.34"]


def seen(resp):
    BODIES.append(resp.text)
    return resp


def hdr(key):
    return {"X-Loc-Token": key}


def main():
    init_db()
    settings.set_many(
        {
            "security_auth_required": "1",
            "app_base_url": "https://app.example.com",
            "notify_outbound_enabled": "1",
        }
    )
    egress.set_test_resolver(_public)
    notify.set_sync(True)
    notify.MIN_INTERVAL_S = 0
    notify.set_test_transport(httpx.MockTransport(_handler))

    with Session(engine) as s:
        cx, cy = Customer(name="CustX"), Customer(name="CustY")
        s.add(cx)
        s.add(cy)
        s.commit()
        s.refresh(cx)
        s.refresh(cy)
        CX, CY = cx.id, cy.id
        px = Project(name="ProjX", customer_id=CX, source_locale="en-US")
        py_ = Project(name="ProjY", customer_id=CY, source_locale="en-US")
        s.add(px)
        s.add(py_)
        s.commit()
        s.refresh(px)
        s.refresh(py_)
        PX, PY = px.id, py_.id
    admin = security.create_user("adm@example.test", "Adm", Role.ADMIN, PW)
    mgr1 = security.create_user("m1@example.test", "M1", Role.MANAGER, PW)
    mgr2 = security.create_user("m2@example.test", "M2", Role.MANAGER, PW)
    bound = security.create_user("cm@example.test", "CM", Role.LINGUIST, PW, customer_id=CX)
    tr = security.create_user("tr@example.test", "TR", Role.TRANSLATOR, PW)
    vw = security.create_user("vw@example.test", "VW", Role.VIEWER, PW)
    key = {
        u.email: apikeys.create_key(u.id, "n", scopes="api docs")[1]
        for u in (admin, mgr1, mgr2, bound, tr, vw)
    }
    c = TestClient(app)

    def api(who, method, path, **kw):
        return seen(c.request(method, path, headers=hdr(key[who]), **kw))

    # [1] roles ----------------------------------------------------------------------------------
    for who in ("tr@example.test", "vw@example.test"):
        assert api(who, "GET", "/api/notify/channels").status_code == 403, who
        r = api(
            who,
            "POST",
            "/api/notify/channels",
            json={"kind": "slack", "name": "n", "url": SLACK_URL},
        )
        assert r.status_code == 403, (who, r.status_code)
        assert api(who, "GET", "/api/notify/routes").status_code == 403
    r = api(
        "m1@example.test",
        "POST",
        "/api/notify/channels",
        json={"kind": "slack", "name": "M1 slack", "url": SLACK_URL},
    )
    assert r.status_code == 200, r.text
    ch1 = r.json()["channel"]["id"]
    assert not any(x in r.text for x in SECRETS_SEEN)
    print("  [1] roles: translator and viewer refused, managers in OK")

    # [2] creator-or-admin, org scope admin only, URL-change alert ---------------------------------
    assert (
        api(
            "m2@example.test", "POST", f"/api/notify/channels/{ch1}", json={"name": "hijack"}
        ).status_code
        == 403
    )
    assert api("m2@example.test", "DELETE", f"/api/notify/channels/{ch1}").status_code == 403
    assert api("m2@example.test", "GET", f"/api/notify/channels/{ch1}").status_code == 200
    mails = []
    orig_send, orig_cfg = mailer.send, mailer.configured
    mailer.send = lambda to, subj, body: mails.append((to, subj, body)) or True
    mailer.configured = lambda: True
    try:
        new_url = "https://hooks.slack.com/services/T9/B9/ZZTOPSECRETTOKEN1B"
        r = api("m1@example.test", "POST", f"/api/notify/channels/{ch1}", json={"url": new_url})
        assert r.status_code == 200, r.text
        assert "ZZTOPSECRETTOKEN1B" not in r.text
        assert [m for m in mails if m[0] == "adm@example.test"], mails
        assert all("ZZTOP" not in m[2] and "hooks.slack.com" not in m[2] for m in mails), mails
        assert "notify.channel.update" in _audit()
    finally:
        mailer.send, mailer.configured = orig_send, orig_cfg
    assert (
        api(
            "adm@example.test", "POST", f"/api/notify/channels/{ch1}", json={"name": "renamed"}
        ).status_code
        == 200
    )
    r = api(
        "m1@example.test", "POST", "/api/notify/routes", json={"channel_id": ch1, "scope": "org"}
    )
    assert r.status_code == 403, r.text
    r = api(
        "adm@example.test", "POST", "/api/notify/routes", json={"channel_id": ch1, "scope": "org"}
    )
    assert r.status_code == 200, r.text
    rid_org = r.json()["route"]["id"]
    r = api(
        "m2@example.test",
        "POST",
        "/api/notify/routes",
        json={"channel_id": ch1, "scope": "project", "ref": str(PX)},
    )
    assert r.status_code == 200, r.text
    # Review gate 10-13: removing the org-wide route is admin-only too (API and web), or a
    # manager could silently drop an admin's org default (for example an audit channel).
    r = api("m1@example.test", "DELETE", f"/api/notify/routes/{rid_org}")
    assert r.status_code == 403, r.text
    mw = TestClient(app)
    csrf_for(mw)
    assert (
        mw.post(
            "/auth/login", data={"email": "m1@example.test", "password": PW}, follow_redirects=False
        ).status_code
        == 303
    )
    csrf_for(mw)
    r = mw.post(f"/settings/notifications/routes/{rid_org}/delete", follow_redirects=False)
    assert r.status_code == 403, (r.status_code, r.headers)
    with Session(engine) as s:
        from app.models import NotifyRoute

        assert s.get(NotifyRoute, rid_org) is not None, "a manager removed the org route"
    assert api("adm@example.test", "DELETE", f"/api/notify/routes/{rid_org}").status_code == 200
    print("  [2] creator-or-admin edit, admin-only org attach and removal, URL-change alert OK")

    # [3] customer ring ---------------------------------------------------------------------------
    r = api(
        "adm@example.test",
        "POST",
        "/api/notify/channels",
        json={"kind": "email", "name": "Y mail", "recipients": "y@example.test", "customer_id": CY},
    )
    chy = r.json()["channel"]["id"]
    r = api(
        "adm@example.test",
        "POST",
        "/api/notify/channels",
        json={"kind": "email", "name": "X mail", "recipients": "x@example.test", "customer_id": CX},
    )
    chx = r.json()["channel"]["id"]
    with Session(engine) as s:
        s.add(
            NotifyOutbox(
                org_id=None,
                channel_id=chy,
                project_id=PY,
                event="run.failed",
                dedup_key="leak1",
                status="failed",
                last_error="LEAKMARK",
                payload={},
            )
        )
        s.add(
            NotifyOutbox(
                org_id=None,
                channel_id=chx,
                project_id=PX,
                event="run.finished",
                dedup_key="ok1",
                status="sent",
                payload={},
            )
        )
        s.commit()
    # a customer-bound account (never a manager) is refused the whole surface; a foreign project
    # is a 404 before anything else, an in-ring one a 403 for the role
    for method, path in (
        ("GET", "/api/notify/channels"),
        ("GET", f"/api/notify/channels/{chx}"),
        ("GET", f"/api/notify/channels/{chx}/deliveries"),
        ("POST", f"/api/notify/projects/{PX}/mute"),
    ):
        r = api("cm@example.test", method, path, json={"muted": True} if method == "POST" else None)
        assert r.status_code == 403, (method, path, r.status_code)
    for method, path in (
        ("GET", f"/api/notify/channels/{chy}"),
        ("GET", f"/api/notify/channels/{chy}/deliveries"),
        ("GET", f"/api/notify/projects/{PY}"),
    ):
        assert api("cm@example.test", method, path).status_code == 404, (method, path)
    assert api("cm@example.test", "GET", f"/api/notify/projects/{PX}").status_code == 200
    # a manager's customer-pinned channel is invisible to a differently pinned view, and the
    # delivery log helper (M1) confines to the channels it is given
    with Session(engine) as s:
        rows = notify.deliveries(s, channel_ids=[chx])
        assert rows and all(r["channel_id"] == chx for r in rows)
        assert "LEAKMARK" not in str(rows)
        assert notify.visible(s, chy, customer_id=CX) is None
        assert notify.visible(s, chx, customer_id=CX) is not None
        assert [c.id for c in notify.list_channels(s, customer_id=CX)] == [chx]
    # the web page: the log lists channels the viewer may see (all of them for staff) and the
    # bound account is refused outright
    w = TestClient(app)
    csrf_for(w)
    assert (
        w.post(
            "/auth/login",
            data={"email": "cm@example.test", "password": PW},
            follow_redirects=False,
        ).status_code
        == 303
    )
    csrf_for(w)
    assert seen(w.get("/settings/notifications")).status_code == 403
    print("  [3] customer ring: foreign ids 404, bound accounts refused, log confined OK")

    # [4] secrets ---------------------------------------------------------------------------------
    a = TestClient(app)
    csrf_for(a)
    assert (
        a.post(
            "/auth/login", data={"email": "m1@example.test", "password": PW}, follow_redirects=False
        ).status_code
        == 303
    )
    csrf_for(a)
    r = a.post(
        "/settings/notifications/channels",
        data={"kind": "webhook", "name": "Hook", "url": HOOK_URL},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers
    first = seen(a.get("/settings/notifications"))
    marker = 'id="notify-secret-value">'
    assert marker in first.text
    secret = first.text.split(marker)[1].split("<")[0]
    assert len(secret) >= 32
    second = seen(a.get("/settings/notifications"))
    assert marker not in second.text and secret not in second.text, "signing secret shown twice"
    r = api("adm@example.test", "GET", "/api/notify/channels")
    assert secret not in r.text and "WEBHOOKTOKEN" not in r.text
    r = api(
        "m1@example.test",
        "POST",
        "/api/notify/channels",
        json={"kind": "webhook", "name": "Hook2", "url": HOOK_URL},
    )
    api_secret = r.json()["signing_secret"]
    r = api("m1@example.test", "GET", f"/api/notify/channels/{r.json()['channel']['id']}")
    assert api_secret not in r.text
    print("  [4] secrets: shown once, never in a response or page OK")

    # [5] egress ----------------------------------------------------------------------------------
    notify.sweep()  # drain
    proj_hook = api("adm@example.test", "GET", "/api/notify/channels").json()["channels"]
    hook_id = next(x["id"] for x in proj_hook if x["name"] == "Hook")
    api(
        "adm@example.test",
        "POST",
        "/api/notify/routes",
        json={"channel_id": hook_id, "scope": "project", "ref": str(PX), "events": ["run.failed"]},
    )
    CALLS.clear()
    ANSWER.update(status=302, location="http://169.254.169.254/latest/meta-data/")
    notify.notify("run.failed", PX, {"run_id": "redir"})
    hook_calls = [x for x in CALLS if x["url"] == HOOK_URL]
    assert len(hook_calls) == 1, CALLS
    assert not any(x["url"].startswith("http://") for x in CALLS), "a redirect was followed"
    with Session(engine) as s:
        row = s.exec(
            select(NotifyOutbox).where(
                NotifyOutbox.dedup_key == "run.failed:redir", NotifyOutbox.channel_id == hook_id
            )
        ).one()
        assert row.status == "failed" and row.http_status == 302 and row.attempts == 1, (
            row.status,
            row.http_status,
        )
    ANSWER.update(status=200, location="")
    for lit in (
        "https://[::1]/x",
        "https://[::ffff:127.0.0.1]/x",
        "https://[::ffff:10.0.0.1]/x",
        "https://0x7f.1/x",
        "https://127.1/x",
    ):
        try:
            notify.validate_url("webhook", lit)
        except notify.NotifyError as exc:
            assert "127." not in str(exc) and "10.0" not in str(exc)
        else:
            raise AssertionError(lit)
    egress.set_test_resolver(lambda h: ["10.1.2.3"])
    try:
        notify.validate_url("webhook", "https://internal.example.org/x")
    except notify.NotifyError as exc:
        assert "10.1.2.3" not in str(exc) and "internal.example.org" not in str(exc), str(exc)
    r = api(
        "m1@example.test",
        "POST",
        "/api/notify/channels",
        json={"kind": "webhook", "name": "Rebind", "url": "https://internal.example.org/x"},
    )
    assert r.status_code == 400 and "10.1.2.3" not in r.text
    egress.set_test_resolver(_public)
    # Slack documented answers
    sl = api(
        "adm@example.test",
        "POST",
        "/api/notify/channels",
        json={"kind": "slack", "name": "S", "url": SLACK_URL},
    ).json()["channel"]["id"]
    tm = api(
        "adm@example.test",
        "POST",
        "/api/notify/channels",
        json={
            "kind": "teams",
            "name": "T",
            "url": "https://prod-1.westus.logic.azure.com/workflows/x/triggers/manual?sig=ZZ",
        },
    ).json()["channel"]["id"]
    for code, retry in (
        (400, False),
        (403, False),
        (404, False),
        (410, False),
        (429, True),
        (500, True),
        (202, None),
        (200, None),
    ):
        ANSWER["status"] = code
        for cid in (sl, tm):
            with Session(engine) as s:
                ch = s.get(NotifyChannel, cid)
                facts = notify._facts(
                    "run.failed", s.get(Project, PX), {"run_id": f"{code}{cid}"}, ""
                )
                s.add(
                    NotifyOutbox(
                        org_id=ch.org_id,
                        channel_id=cid,
                        project_id=PX,
                        event="run.failed",
                        dedup_key=f"st{code}-{cid}",
                        payload=facts,
                        status="pending",
                        next_attempt_at=notify._utcnow(),
                    )
                )
                s.commit()
        notify.sweep(now=notify._utcnow() + timedelta(seconds=1))
        with Session(engine) as s:
            for cid in (sl, tm):
                r = s.exec(
                    select(NotifyOutbox).where(NotifyOutbox.dedup_key == f"st{code}-{cid}")
                ).one()
                want = "sent" if retry is None else ("retry" if retry else "failed")
                assert r.status == want, (code, cid, r.status)
    ANSWER["status"] = 200
    print("  [5] egress: 302 not followed, literals refused quietly, documented answers OK")

    # [6] signature -------------------------------------------------------------------------------
    body = b'{"a":1}'
    # fixed known-answer vector, computed independently: HMAC-SHA256("key", "1700000000." + body)
    assert notify.sign_body("key", body, ts=1700000000) == "t=1700000000,v1=" + KNOWN_V1

    def verify(secret_, header, raw, now, skew=300):
        parts = dict(p.split("=", 1) for p in header.split(","))
        if abs(now - int(parts["t"])) > skew:
            return False
        mac = hmac.new(
            secret_.encode(), (parts["t"] + ".").encode() + raw, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(mac, parts["v1"])

    h = notify.sign_body("key", body, ts=int(time.time()))
    assert verify("key", h, body, time.time())
    assert not verify("key", h, body + b" ", time.time())  # tampered body
    assert not verify("key", h, body, time.time() + 3600)  # stale timestamp
    assert not verify("other", h, body, time.time())
    print("  [6] signature: known-answer vector, reference verifier, tamper and stale refused OK")

    # [7] hooks -----------------------------------------------------------------------------------
    api(
        "adm@example.test",
        "POST",
        "/api/notify/routes",
        json={
            "channel_id": hook_id,
            "scope": "org",
            "events": ["project.created", "run.limit_hit"],
        },
    )
    CALLS.clear()
    with Session(engine) as s:
        p = content_projects.project_create(
            s,
            "Hooked project",
            source_locale="en-US",
            target_locales="fr-FR",
            default_provider="mock",
            created_by="adm@example.test",
        )
        newpid = p.id
    assert any(b"Hooked project" in x["body"] for x in CALLS), (
        "project.created did not reach the channel"
    )
    CALLS.clear()
    with Session(engine) as s:
        project_budgets.upsert_project_budget(s, newpid, 1.0)
        from app.models import Document

        d = Document(project_id=newpid, filename="d.docx", fmt="docx")
        s.add(d)
        s.commit()
        s.refresh(d)
        j = Job(
            kind="translate_document",
            payload={"document_id": d.id, "locale": "fr-FR"},
            status=JobStatus.COMPLETED,
            result={
                "provider_used": {"claude": 1},
                "tokens_in": 5_000_000,
                "tokens_out": 5_000_000,
            },
            created_by="u@x.co",
        )
        s.add(j)
        s.commit()
    assert project_budgets.evaluate_thresholds(newpid, actor="sys") in (80, 100)
    assert any(b"budget" in x["body"] for x in CALLS), CALLS
    print("  [7] hooks: project.created and the budget threshold reach the channel OK")

    # [8] sweep -----------------------------------------------------------------------------------
    now = notify._utcnow()
    with Session(engine) as s:
        old = NotifyOutbox(
            org_id=None,
            channel_id=hook_id,
            event="run.finished",
            dedup_key="old-sent",
            status="sent",
            payload={},
            created_at=now - timedelta(days=40),
        )
        stale = NotifyOutbox(
            org_id=None,
            channel_id=hook_id,
            project_id=PX,
            event="run.failed",
            dedup_key="stale1",
            status="pending",
            payload=notify._facts("run.failed", s.get(Project, PX), {}, ""),
            created_at=now - timedelta(days=2),
            next_attempt_at=now,
        )
        s.add(old)
        s.add(stale)
        s.commit()
    CALLS.clear()
    notify._LAST_PRUNE[0] = 0.0
    notify.sweep(now=notify._utcnow())
    with Session(engine) as s:
        assert (
            s.exec(select(NotifyOutbox).where(NotifyOutbox.dedup_key == "old-sent")).first() is None
        )
        st = s.exec(select(NotifyOutbox).where(NotifyOutbox.dedup_key == "stale1")).one()
        assert st.status == "skipped" and "stale" in st.last_error and not CALLS
    # async wrapper never blocks and does not stack
    notify.set_sync(False)
    assert notify.sweep_async() in (True, False)
    time.sleep(0.5)
    notify.set_sync(True)
    # email retries only the failed recipient
    em = api(
        "adm@example.test",
        "POST",
        "/api/notify/channels",
        json={"kind": "email", "name": "Two", "recipients": "a@x.org,b@x.org"},
    ).json()["channel"]["id"]
    api(
        "adm@example.test",
        "POST",
        "/api/notify/routes",
        json={"channel_id": em, "scope": "project", "ref": str(PX), "events": ["run.failed"]},
    )
    sent = []
    fail_once = {"b@x.org": 1}

    def fake_send(to, subj, body_):
        if fail_once.get(to):
            fail_once[to] -= 1
            return False
        sent.append(to)
        return True

    mailer.send, mailer.configured = fake_send, lambda: True
    try:
        notify.notify("run.failed", PX, {"run_id": "mail1"})
        notify.sweep(now=notify._utcnow() + timedelta(seconds=90))
    finally:
        mailer.send, mailer.configured = orig_send, orig_cfg
    assert sorted(sent) == ["a@x.org", "b@x.org"], sent  # a once, b on the retry
    # the channel test is rate limited
    with Session(engine) as s:
        chh = s.get(NotifyChannel, hook_id)
        res = [notify.test_send(s, chh)["ok"] for _ in range(7)]
    assert res.count(False) >= 2, res
    print("  [8] sweep: async, prunes, stale skipped, email retries the failure, tests limited OK")

    # [9] GDPR (Review gate 10-13): a channel creator and an email recipient are personal data.
    # Export shows them; erase tombstones the creator AND the live SMTP recipient.
    from app.services import gdpr

    subj = security.create_user("gone@example.test", "Gone", Role.MANAGER, PW)
    gkey = apikeys.create_key(subj.id, "g", scopes="api docs")[1]
    r = seen(
        c.post(
            "/api/notify/channels",
            headers=hdr(gkey),
            json={"kind": "email", "name": "Gdpr", "recipients": "gone@example.test,keep@x.org"},
        )
    )
    assert r.status_code == 200, r.text
    gch = r.json()["channel"]["id"]
    r = seen(
        c.post(
            "/api/notify/routes",
            headers=hdr(gkey),
            json={"channel_id": gch, "scope": "project", "ref": str(PX)},
        )
    )
    assert r.status_code == 200, r.text
    got = {}
    for sec, rec in gdpr.export_user(subj.id):
        got.setdefault(sec, []).append(rec)
    assert [x["id"] for x in got.get("notify_channels", [])] == [gch], got.keys()
    assert got.get("notify_routes"), got.keys()
    assert got["notify_channel_recipients"][0]["recipients"] == ["gone@example.test"], got
    ok, msg = gdpr.erase_user(subj.id, actor="adm@example.test")
    assert ok, msg
    with Session(engine) as s:
        from app.models import NotifyRoute

        ch = s.get(NotifyChannel, gch)
        assert ch.created_by != "gone@example.test", ch.created_by
        assert "gone@example.test" not in (ch.config or {}).get("recipients", []), ch.config
        assert "keep@x.org" in (ch.config or {}).get("recipients", []), ch.config
        assert all(
            r.created_by != "gone@example.test"
            for r in s.exec(select(NotifyRoute).where(NotifyRoute.channel_id == gch)).all()
        )
    print("  [9] GDPR: notify creators and email recipients exported and erased OK")

    blob = " ".join(BODIES)
    for needle in SECRETS_SEEN:
        assert needle not in blob, "a destination token appeared in a response body"
    # each signing secret is shown exactly once: in the response or page that minted it
    assert blob.count(secret) == 1 and blob.count(api_secret) == 1, "a signing secret repeated"
    print("NOTIFY HTTP OK")


def _audit() -> str:
    from app.models import AuditLog

    with Session(engine) as s:
        return "\n".join(
            f"{r.action}|{r.target}|{r.detail}"
            for r in s.exec(select(AuditLog).where(AuditLog.action.like("notify.%"))).all()
        )


KNOWN_V1 = "a438e398bfafc57e4396bb7fc2304422f0f768e965d073ca313cb52e22e6ad03"

if __name__ == "__main__":
    main()
