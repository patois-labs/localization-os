"""Project notifications (Part B): channels, routing, delivery, hooks (offline, fakes only).

Documented in: docs/architecture/notifications.md

Every send goes to an httpx.MockTransport (the documented Slack / Teams / webhook responses); no
real message leaves the process and no credentials are used.

  [1] channel validation: Slack pinned to hooks.slack.com, Teams to Power Platform hosts, https
      only, no user info, private and loopback hosts refused by the SSRF guard, bad recipients
  [2] secrets: the URL and signing secret are Fernet at rest, never in to_dict, the audit rows or
      the delivery log; the webhook secret is shown once and rotates
  [3] routing: project > content type > project type > customer > org; most specific wins per
      kind; events default; mute; customer-pinned channel
  [4] messages: Slack mrkdwn and Teams escaping (`<!channel>` cannot ping), a status link, NEVER
      content text, the signed webhook verifies
  [5] delivery: success, transient retry at 1/5/30 minutes then failed, a 4xx fails at once,
      dedup per (run, event), kill switch, disabled channel, email adapter, test-send
  [6] hooks: project completed/created, connector job started/finished/failed/needs-review/limit,
      budget threshold, human-review pause
"""

import hashlib
import hmac
import json
import os
import sys
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_notify_")

import httpx
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    AuditLog,
    ContentType,
    Customer,
    Job,
    JobStatus,
    NotifyChannel,
    NotifyOutbox,
    NotifyRoute,
    Project,
    ProjectStatus,
)
from app.services import egress, lifecycle, mailer, notify, settings

CALLS: list[dict] = []
ANSWER = {"status": 200}


def _handler(request: httpx.Request) -> httpx.Response:
    CALLS.append(
        {
            "url": str(request.url),
            "body": request.content,
            "headers": dict(request.headers),
        }
    )
    return httpx.Response(ANSWER["status"], text="ok")


def _public(host):
    return ["93.184.216.34"]


SLACK_URL = "https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXSECRET1"
TEAMS_URL = "https://prod-12.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=SECRETSIG"
HOOK_URL = "https://hooks.example.org/in/abcdef123456"


def _expect(exc_type, fn, needle=""):
    try:
        fn()
    except exc_type as exc:
        assert needle.lower() in str(exc).lower(), (needle, str(exc))
        return
    raise AssertionError(f"expected {exc_type.__name__} containing {needle!r}")


def _reset():
    CALLS.clear()
    ANSWER["status"] = 200
    with Session(engine) as s:
        for m in (NotifyOutbox, NotifyRoute, NotifyChannel):
            for row in s.exec(select(m)).all():
                s.delete(row)
            s.flush()
        s.commit()


def _audit_text() -> str:
    with Session(engine) as s:
        return "\n".join(
            f"{r.action}|{r.target}|{r.detail}"
            for r in s.exec(select(AuditLog).where(AuditLog.action.like("notify.%"))).all()
        )


def test_validation(s):
    ok = lambda kind, url: notify.validate_url(kind, url)  # noqa: E731
    assert ok("slack", SLACK_URL) == SLACK_URL
    _expect(notify.NotifyError, lambda: ok("slack", "http://hooks.slack.com/services/x"), "https")
    _expect(
        notify.NotifyError, lambda: ok("slack", "https://evil.example.com/x"), "hooks.slack.com"
    )
    _expect(notify.NotifyError, lambda: ok("slack", "https://hooks.slack.com.evil.io/x"), "hooks")
    _expect(notify.NotifyError, lambda: ok("slack", "https://u:p@hooks.slack.com/x"), "user name")
    _expect(notify.NotifyError, lambda: ok("slack", "https://hooks.slack.com:8443/x"), "port")
    assert ok("teams", TEAMS_URL) == TEAMS_URL
    _expect(notify.NotifyError, lambda: ok("teams", "https://hooks.slack.com/x"), "workflows")
    # generic webhook: any https host, but the SSRF guard refuses private and loopback targets
    assert ok("webhook", HOOK_URL) == HOOK_URL
    _expect(notify.NotifyError, lambda: ok("webhook", "https://127.0.0.1/x"), "not allowed")
    _expect(
        notify.NotifyError, lambda: ok("webhook", "https://169.254.169.254/latest"), "not allowed"
    )
    egress.set_test_resolver(lambda h: ["10.0.0.5"])
    _expect(
        notify.NotifyError, lambda: ok("webhook", "https://internal.example.org/x"), "not allowed"
    )
    egress.set_test_resolver(_public)
    _expect(notify.NotifyError, lambda: notify._recipients("not-an-email"), "valid address")
    _expect(notify.NotifyError, lambda: notify._recipients([]), "recipient")
    assert notify._recipients("a@x.org, b@x.org a@X.org") == ["a@x.org", "b@x.org"]
    # kind and name
    _expect(
        notify.NotifyError,
        lambda: notify.create_channel(s, kind="pager", name="x", url=HOOK_URL),
        "unknown channel kind",
    )
    _expect(
        notify.NotifyError,
        lambda: notify.create_channel(s, kind="slack", name="  ", url=SLACK_URL),
        "name",
    )
    print("  [1] validation: host pins, https only, SSRF guard, recipients OK")


def test_secrets(s):
    _reset()
    ch, shown = notify.create_channel(
        s, kind="slack", name="Ops", url=SLACK_URL, created_by="a@x.org"
    )
    assert shown == ""
    assert SLACK_URL not in ch.secret_enc and "SECRET1" not in ch.secret_enc
    d = notify.to_dict(s, ch)
    assert "SECRET1" in d["destination"] or d["destination"].endswith("CRET1"), d
    assert SLACK_URL not in json.dumps(d) and "services/T000" not in json.dumps(d)
    wh, secret = notify.create_channel(s, kind="webhook", name="Hook", url=HOOK_URL)
    assert secret and len(secret) >= 32
    assert secret not in wh.secret_enc and secret not in json.dumps(notify.to_dict(s, wh))
    assert notify.to_dict(s, wh)["has_signing_secret"] is True
    new = notify.update_channel(s, wh, rotate_signing_secret=True, actor="a@x.org")
    assert new and new != secret and notify.secret_dict(wh)["signing_secret"] == new
    _expect(
        notify.NotifyError,
        lambda: notify.update_channel(s, ch, rotate_signing_secret=True),
        "webhook",
    )
    notify.update_channel(
        s, ch, url="https://hooks.slack.com/services/T9/B9/NEWTOKEN", actor="a@x.org"
    )
    assert notify.secret_dict(ch)["url"].endswith("NEWTOKEN")
    _expect(
        notify.NotifyError,
        lambda: notify.create_channel(s, kind="slack", name="ops", url=SLACK_URL),
        "already",
    )
    audit = _audit_text()
    for needle in ("SECRET1", "NEWTOKEN", secret, new, "services/T"):
        assert needle not in audit, needle
    assert "notify.channel.create" in audit and "notify.channel.update" in audit
    print("  [2] credentials: Fernet at rest, masked on every surface, shown once, audit clean OK")


def _project(s, name="Northwind", customer=None, ctype=None, ptype="document"):
    p = Project(
        name=name,
        source_locale="en-US",
        customer_id=customer,
        content_type_id=ctype,
        project_type=ptype,
        created_by="t@x.org",
    )
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def test_routing(s):
    _reset()
    cust = Customer(name="Contoso")
    s.add(cust)
    s.commit()
    s.refresh(cust)
    other = Customer(name="Fabrikam")
    s.add(other)
    s.commit()
    s.refresh(other)
    ct = ContentType(name="Marketing")
    s.add(ct)
    s.commit()
    s.refresh(ct)
    CT = ct.id
    proj = _project(s, customer=cust.id, ctype=CT)
    slack_org, _ = notify.create_channel(s, kind="slack", name="org-slack", url=SLACK_URL)
    slack_ct, _ = notify.create_channel(s, kind="slack", name="ct-slack", url=SLACK_URL)
    slack_proj, _ = notify.create_channel(s, kind="slack", name="proj-slack", url=SLACK_URL)
    teams_cust, _ = notify.create_channel(s, kind="teams", name="cust-teams", url=TEAMS_URL)
    hook_type, _ = notify.create_channel(s, kind="webhook", name="type-hook", url=HOOK_URL)
    notify.set_route(s, scope="org", ref="", channel=slack_org)
    notify.set_route(s, scope="content_type", ref=CT, channel=slack_ct)
    notify.set_route(s, scope="customer", ref=cust.id, channel=teams_cust)
    notify.set_route(s, scope="project_type", ref="document", channel=hook_type)

    def names(event):
        return sorted(c.name for c, _ in notify.resolve(s, proj, event))

    # slack: content type beats org; teams from customer; webhook from project type
    assert names("run.finished") == ["ct-slack", "cust-teams", "type-hook"], names("run.finished")
    notify.set_route(s, scope="project", ref=proj.id, channel=slack_proj, project=proj)
    assert names("run.finished") == ["cust-teams", "proj-slack", "type-hook"]
    # defaults: run.started is off unless a route names it
    assert names("run.started") == []
    notify.set_route(
        s, scope="project", ref=proj.id, channel=slack_proj, events=["run.started"], project=proj
    )
    assert names("run.started") == ["proj-slack"]
    # the project route no longer covers run.finished, so the content type route speaks again
    assert "ct-slack" in names("run.finished") and "proj-slack" not in names("run.finished")
    eff = notify.effective(s, proj)
    assert eff["events"]["run.started"][0]["from"] == "project", eff
    # unknown event and unknown scope are refused
    _expect(
        notify.NotifyError,
        lambda: notify.set_route(
            s, scope="project", ref=proj.id, channel=slack_proj, events=["nope"], project=proj
        ),
        "unknown event",
    )
    _expect(
        notify.NotifyError,
        lambda: notify.set_route(s, scope="galaxy", ref=1, channel=slack_org),
        "unknown scope",
    )
    # disabled channel is skipped
    notify.update_channel(s, hook_type, enabled=False)
    assert "type-hook" not in names("run.finished")
    notify.update_channel(s, hook_type, enabled=True)
    # customer-pinned channel
    pinned, _ = notify.create_channel(
        s, kind="slack", name="pinned", url=SLACK_URL, customer_id=other.id
    )
    _expect(
        notify.NotifyError,
        lambda: notify.set_route(s, scope="org", ref="", channel=pinned),
        "customer",
    )
    _expect(
        notify.NotifyError,
        lambda: notify.set_route(s, scope="customer", ref=cust.id, channel=pinned),
        "different customer",
    )
    notify.set_route(s, scope="customer", ref=other.id, channel=pinned)
    assert "pinned" not in names("run.finished")  # not this project's customer
    # mute silences everything
    notify.set_project_muted(s, proj, True, actor="a@x.org")
    assert names("run.finished") == [] and notify.effective(s, proj)["muted"] is True
    assert notify.notify("run.finished", proj, {"run_id": 1}) == 0
    notify.set_project_muted(s, proj, False, actor="a@x.org")
    assert names("run.finished")
    print("  [3] routing: five levels, per-kind winner, events, mute, disabled, customer pin OK")


def test_messages(s):
    _reset()
    proj = _project(s, name="<!channel> Launch & <b>Docs</b>")
    facts = notify._facts(
        "run.finished",
        proj,
        {
            "counts": {"written": 38, "held": 6, "failed": 1, "created": 14},
            "at": notify._utcnow().replace(hour=9, minute=5),
        },
        "Acme <@U1>",
    )
    settings.set_many({"app_base_url": "https://app.example.com"})
    facts["url"] = notify._status_url(proj.id)
    sl = notify.render_slack(facts)
    assert "<!channel>" not in sl["text"] and "&lt;!channel&gt;" in sl["text"], sl
    assert "&lt;@U1&gt;" in sl["text"]
    assert f"<https://app.example.com/projects/{proj.id}|Open project>" in sl["text"]
    assert (
        "38 written" in sl["text"]
        and "6 waiting for review" in sl["text"]
        and "9:05 AM UTC" in sl["text"]
    )
    tm = notify.render_teams(facts)
    card = tm["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard" and card["actions"][0]["url"].endswith(
        f"/projects/{proj.id}"
    )
    flat = json.dumps(tm)
    assert "<!channel>" not in flat and "\\\\<" in flat  # angle brackets backslash-escaped
    wh = notify.render_webhook(facts, "d1")
    assert wh["event"] == "run.finished" and wh["project"]["url"].endswith(str(proj.id))
    # with no app_base_url there is no link and the text says so
    settings.set_many({"app_base_url": ""})
    f2 = notify._facts("run.failed", proj, {}, "")
    assert f2["url"] == "" and "Open Localization OS" in notify.render_slack(f2)["text"]
    # a limit phrase, and NO cost or content ever enters the facts
    f3 = notify._facts(
        "run.limit_hit",
        proj,
        {
            "limit": "budget",
            "band": 80,
            "cost_usd": 12.5,
            "source_text": "SECRET TEXT",
            "counts": {"written": 2, "source_text": 5, "cost": 9},
        },
        "",
    )
    blob = json.dumps(f3)
    assert "12.5" not in blob and "SECRET TEXT" not in blob and "cost" not in blob
    assert "80%" in notify.render_slack(f3)["text"]
    # signature scheme: t=<unix>,v1=hmac(secret, "t.body")
    body = b'{"a":1}'
    sig = notify.sign_body("s3cret", body, ts=1700000000)
    mac = hmac.new(b"s3cret", b"1700000000." + body, hashlib.sha256).hexdigest()
    assert sig == f"t=1700000000,v1={mac}"
    print("  [4] messages: escaping, link, counts, no content or cost, signature scheme OK")


def test_delivery(s):
    _reset()
    notify.set_sync(True)
    notify.MIN_INTERVAL_S = 0
    notify.set_test_transport(httpx.MockTransport(_handler))
    egress.set_test_resolver(_public)
    settings.set_many({"app_base_url": "https://app.example.com", "notify_outbound_enabled": "1"})
    proj = _project(s, name="Delivery")
    ch, secret = notify.create_channel(s, kind="webhook", name="Hook", url=HOOK_URL)
    sl, _ = notify.create_channel(s, kind="slack", name="Chat", url=SLACK_URL)
    tm, _ = notify.create_channel(s, kind="teams", name="Teams", url=TEAMS_URL)
    for c in (ch, sl, tm):
        notify.set_route(s, scope="org", ref="", channel=c)
    # one event -> three deliveries, signed webhook verifies against the shown secret
    n = notify.notify("run.finished", proj, {"run_id": 11, "counts": {"written": 3}})
    assert n == 3 and len(CALLS) == 3, (n, len(CALLS))
    hook_call = next(c for c in CALLS if c["url"] == HOOK_URL)
    t, v1 = (p.split("=", 1)[1] for p in hook_call["headers"]["x-locos-signature"].split(","))
    assert (
        hmac.new(secret.encode(), f"{t}.".encode() + hook_call["body"], hashlib.sha256).hexdigest()
        == v1
    )
    assert hook_call["headers"]["x-locos-event"] == "run.finished"
    assert json.loads(hook_call["body"])["counts"] == {"written": 3}
    assert all(r.status == "sent" for r in s.exec(select(NotifyOutbox)).all())
    # dedup: the same (run, event) is one message
    assert notify.notify("run.finished", proj, {"run_id": 11}) == 0 and len(CALLS) == 3
    assert notify.notify("run.finished", proj, {"run_id": 12}) == 3
    # transient failure -> retry ladder 1m, 5m, 30m -> failed (visible in the log)
    CALLS.clear()
    ANSWER["status"] = 503
    s.expire_all()
    for r in s.exec(select(NotifyOutbox)).all():
        s.delete(r)
    s.commit()
    notify.delete_channel(s, tm)
    notify.delete_channel(s, sl)
    assert notify.notify("run.failed", proj, {"run_id": 20}) == 1
    row = s.exec(select(NotifyOutbox)).one()
    s.refresh(row)
    assert (row.status, row.attempts) == ("retry", 1), (row.status, row.attempts)
    gap = (row.next_attempt_at - notify._utcnow()).total_seconds()
    assert 50 <= gap <= 61, gap
    assert notify.sweep(now=notify._utcnow()) == 0  # not due yet
    for expect_gap in (300, 1800):
        due = row.next_attempt_at + timedelta(seconds=1)
        notify.sweep(now=due)
        s.refresh(row)
        if expect_gap == 300:
            assert (row.status, row.attempts) == ("retry", 2)
        else:
            assert (row.status, row.attempts) == ("retry", 3)
    notify.sweep(now=row.next_attempt_at + timedelta(seconds=1))
    s.refresh(row)
    assert (row.status, row.attempts, row.http_status) == ("failed", 4, 503), (
        row.status,
        row.attempts,
    )
    assert "notify.delivery_failed" in _audit_text()
    assert notify.deliveries(s, channel_id=ch.id)[0]["status"] == "failed"
    # "retry failed" puts it back and a healthy destination then delivers it
    ANSWER["status"] = 200
    assert notify.retry_failed(s, ch) == 1
    notify.sweep(now=notify._utcnow() + timedelta(seconds=5))
    s.refresh(row)
    assert row.status == "sent"
    # a 4xx is permanent: failed on the first attempt, no retry
    ANSWER["status"] = 404
    notify.notify("run.failed", proj, {"run_id": 21})
    r2 = s.exec(select(NotifyOutbox).where(NotifyOutbox.dedup_key == "run.failed:21")).one()
    assert (r2.status, r2.attempts, r2.http_status) == ("failed", 1, 404)
    # a private destination (rebinding) is refused with no dial
    ANSWER["status"] = 200
    CALLS.clear()
    egress.set_test_resolver(lambda h: ["10.1.2.3"])
    notify.notify("run.failed", proj, {"run_id": 22})
    r3 = s.exec(select(NotifyOutbox).where(NotifyOutbox.dedup_key == "run.failed:22")).one()
    assert r3.status == "failed" and CALLS == [] and "refused" in r3.last_error, r3.last_error
    egress.set_test_resolver(_public)
    # kill switch: nothing queued, nothing sent
    settings.set_many({"notify_outbound_enabled": "0"})
    before = len(s.exec(select(NotifyOutbox)).all())
    assert notify.notify("run.failed", proj, {"run_id": 23}) == 0
    assert len(s.exec(select(NotifyOutbox)).all()) == before
    assert notify.sweep() == 0
    res = notify.test_send(s, ch)
    assert res["ok"] is False and "switched off" in res["message"]
    settings.set_many({"notify_outbound_enabled": "1"})
    # test-send
    CALLS.clear()
    res = notify.test_send(s, ch, actor="a@x.org")
    assert res["ok"] and len(CALLS) == 1 and ch.last_test_ok is True
    assert "test" in json.loads(CALLS[0]["body"])["summary"].lower()
    ANSWER["status"] = 500
    res = notify.test_send(s, ch)
    assert not res["ok"] and "500" in res["message"] and ch.last_test_ok is False
    ANSWER["status"] = 200
    # email adapter
    em, _ = notify.create_channel(s, kind="email", name="Mail", recipients="a@x.org,b@x.org")
    sent = []
    orig_send, orig_cfg = mailer.send, mailer.configured
    mailer.send = lambda to, subj, body: sent.append((to, subj, body)) or True
    mailer.configured = lambda: True
    try:
        notify.set_route(s, scope="project", ref=proj.id, channel=em, project=proj)
        notify.notify("run.failed", proj, {"run_id": 30, "counts": {"failed": 2}})
        mail = [m for m in sent if "Delivery" in m[1]]
        assert {m[0] for m in mail} == {"a@x.org", "b@x.org"}, sent
        assert "/projects/" in mail[0][2] or "Open Localization OS" in mail[0][2]
        mailer.configured = lambda: False
        notify.notify("run.failed", proj, {"run_id": 31})
        r = s.exec(
            select(NotifyOutbox).where(
                NotifyOutbox.dedup_key == "run.failed:31", NotifyOutbox.channel_id == em.id
            )
        ).one()
        assert r.status == "failed" and "SMTP" in r.last_error
    finally:
        mailer.send, mailer.configured = orig_send, orig_cfg
    # a disabled channel is skipped at delivery too
    notify.delete_channel(s, ch)
    assert s.exec(select(NotifyOutbox).where(NotifyOutbox.channel_id == ch.id)).all() == []
    print("  [5] delivery: signed webhook, dedup, 1/5/30 retries, 4xx, SSRF, kill switch, email OK")


def test_hooks(s):
    _reset()
    notify.set_sync(True)
    settings.set_many({"notify_outbound_enabled": "1", "app_base_url": "https://app.example.com"})
    proj = _project(s, name="Hooked")
    ch, _ = notify.create_channel(s, kind="slack", name="All", url=SLACK_URL)
    notify.set_route(
        s,
        scope="org",
        ref="",
        channel=ch,
        events=[
            "run.started",
            "run.finished",
            "run.failed",
            "run.needs_review",
            "run.limit_hit",
            "project.created",
            "project.completed",
        ],
    )

    def events():
        s.expire_all()
        return sorted(r.event for r in s.exec(select(NotifyOutbox)).all())

    CALLS.clear()
    # project status -> completed
    lifecycle.set_status(s, proj.id, ProjectStatus.COMPLETED, actor="a@x.org")
    assert events() == ["project.completed"], events()
    lifecycle.set_status(s, proj.id, ProjectStatus.COMPLETED, actor="a@x.org")  # no-op: still one
    assert events() == ["project.completed"]
    # connector job hooks
    from app.models import Connection

    conn = Connection(kind="jira", name="J", project_id=proj.id)
    s.add(conn)
    s.commit()
    s.refresh(conn)

    def job(status, result):
        j = Job(
            kind="connector_translate",
            status=status,
            payload={"connection_id": conn.id},
            result=result,
            created_by="t",
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id

    jid = job(JobStatus.RUNNING, {})
    notify.on_connector_job(jid, "started")
    jid2 = job(
        JobStatus.COMPLETED,
        {"created": 4, "updated": 2, "held_back": 3, "ceiling_hit": True, "failures": [{"x": 1}]},
    )
    notify.on_connector_job(jid2, "settled")
    jid3 = job(JobStatus.COMPLETED, {"created": 0, "updated": 0})
    notify.on_connector_job(jid3, "settled")  # empty run: silent
    jid4 = job(JobStatus.FAILED, {})
    notify.on_connector_job(jid4, "settled")
    got = events()
    assert got == sorted(
        [
            "project.completed",
            "run.started",
            "run.finished",
            "run.needs_review",
            "run.limit_hit",
            "run.failed",
        ]
    ), got
    body = [json.loads(c["body"]) for c in CALLS if b"4 created" in c["body"]]
    assert body, [c["body"] for c in CALLS]
    # a job with no project, or a non-connector job, is ignored; dedup per job
    notify.on_connector_job(jid2, "settled")
    assert events() == got
    j = Job(kind="translate_document", status=JobStatus.COMPLETED, payload={}, result={})
    s.add(j)
    s.commit()
    s.refresh(j)
    notify.on_connector_job(j.id, "settled")
    assert events() == got
    # budget threshold + human review paused go through the same interface
    from app.services import notifications

    class _Run:
        id = 5
        document_id = None

    notifications._notify_needs_review(_Run(), type("S", (), {"id": 1})())
    assert events() == got  # no document -> no project -> nothing
    print("  [6] hooks: project completed, connector job phases, silence for empty runs OK")


def main():
    init_db()
    with Session(engine) as s:
        egress.set_test_resolver(_public)
        notify.set_sync(True)
        notify.MIN_INTERVAL_S = 0
        test_validation(s)
        test_secrets(s)
        test_routing(s)
        test_messages(s)
        test_delivery(s)
        test_hooks(s)
    print("NOTIFY OK")


if __name__ == "__main__":
    main()
