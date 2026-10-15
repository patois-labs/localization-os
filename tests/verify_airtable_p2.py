"""Airtable connector PHASE 2 verification — the official Webhooks API live path,
fully offline (httpx.MockTransport + the built-in MockProvider). Covers:

  1. REGISTRATION: register_airtable_webhook resolves table/field ids, builds a spec
     (fromSources excluding publicApi + watchDataInFieldIds on the source field),
     POSTs create, VAULTS the returned macSecretBase64 as webhook_secret, and
     persists {id, base_id, source_field_id, cursor:0, expiry} in config.
  2. PUBLIC-URL GUARD: registration is REFUSED (clean, actionable message) when
     app_base_url is unset / http / localhost — Airtable can't reach localhost.
  3. PING -> PAYLOAD PULL: consume_airtable_payloads fetches payloads from the
     stored cursor, extracts only SOURCE-field-changed record ids (target-column
     echoes ignored), advances the cursor after success, and re-translates exactly
     those records. Cursor persists across runs.
  4. TARGET-ECHO ping -> NO translation (the changed field is the target column, not
     the watched source field).
  5. auto_translate_on_change OFF (default) -> pending status only, no translation;
     ON -> targeted job runs. force=True (translate-pending) drains the queue.
  6. KEEP-ALIVE: refresh success updates expiry; a 404 clears the registration
     cleanly (config + vaulted secret) and does NOT hard-error.
  7. ORG SCOPING: the intake -> job path is org-scoped by the connection (a signed
     ping for connection X only ever drives connection X's records).
  8. INTAKE wiring: a bare (empty-id) ping ENQUEUES a payload-pull job (ping-then-
     pull), unlike the non-ping-pull connectors where an empty ping is a no-op.
"""

import base64
import hashlib
import hmac
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config as app_config
from app.db import engine, init_db
from app.main import app
from app.models import Connection, Job, JobStatus
from app.services import settings as app_settings
from app.services.connectors import airtable as at
from app.services.connectors import store as cstore
from app.services.connectors import webhooks


# --- shared MockTransport builder -----------------------------------------------
# One handler covers: meta (table/field id resolution), create/list-payloads/
# refresh/delete webhook, GET records (fetch), PATCH (write-back). Each test tunes
# the payloads it returns via the closure.
def _meta_tables():
    # Notes -> fldSRC (watched source), Notes (es-MX) -> fldTGT (target column)
    return {
        "tables": [
            {
                "id": "tblC",
                "name": "Content",
                "fields": [
                    {"id": "fldSRC", "name": "Notes", "type": "richText"},
                    {"id": "fldTGT", "name": "Notes (es-MX)", "type": "richText"},
                ],
            }
        ]
    }


def _make_transport(state):
    """state: mutable dict controlling responses + capturing calls.
    state['payloads_pages'] : list of list-payloads response dicts (popped FIFO)
    state['records']        : {record_id: source_text} for GET fetch
    state['pushed']         : appended PATCH record bodies
    state['created']        : appended create-webhook bodies
    state['deleted']        : appended (base, webhook_id)
    state['refresh']        : response dict for the refresh POST (or 404 flag)
    """
    base = state["base"]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        m = req.method
        if m == "GET" and path == f"/v0/meta/bases/{base}/tables":
            return httpx.Response(200, json=_meta_tables())
        if m == "POST" and path == f"/v0/bases/{base}/webhooks":
            state.setdefault("created", []).append(json.loads(req.content))
            return httpx.Response(
                200,
                json={
                    "id": "achNEW",
                    "macSecretBase64": state["mac_b64"],
                    "expirationTime": "2026-07-13T00:00:00.000Z",
                },
            )
        if m == "DELETE" and path.startswith(f"/v0/bases/{base}/webhooks/"):
            wh_id = path.rsplit("/", 1)[-1]
            state.setdefault("deleted", []).append((base, wh_id))
            return httpx.Response(200, json={})
        if m == "POST" and path.endswith("/refresh"):
            if state.get("refresh_404"):
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"expirationTime": "2026-07-20T00:00:00.000Z"})
        if m == "GET" and "/payloads" in path:
            if state.get("payloads_404"):
                return httpx.Response(404, json={"error": "gone"})
            pages = state.get("payloads_pages") or []
            resp = (
                pages.pop(0)
                if pages
                else {"payloads": [], "cursor": state.get("last_cursor", 1), "mightHaveMore": False}
            )
            state.setdefault("cursors_seen", []).append(req.url.params.get("cursor"))
            return httpx.Response(200, json=resp)
        if m == "GET" and path.startswith(f"/v0/{base}/"):
            recs = [
                {"id": rid, "fields": {"Notes": txt}}
                for rid, txt in (state.get("records") or {}).items()
            ]
            return httpx.Response(200, json={"records": recs})
        if m == "PATCH" and path.startswith(f"/v0/{base}/"):
            body = json.loads(req.content)
            state.setdefault("pushed", []).extend(body["records"])
            return httpx.Response(200, json={"records": body["records"]})
        return httpx.Response(404, json={"path": path, "method": m})

    return httpx.MockTransport(handler)


def _payloads_page(changed_by_record, cursor, more=False):
    """changed_by_record: {record_id: [field_id,...]} -> a list-payloads page."""
    recs = {}
    for rid, fids in changed_by_record.items():
        recs[rid] = {"current": {"cellValuesByFieldId": dict.fromkeys(fids, "x")}}
    return {
        "payloads": [{"changedTablesById": {"tblC": {"changedRecordsById": recs}}}],
        "cursor": cursor,
        "mightHaveMore": more,
    }


def _mk_conn(name, extra_cfg=None):
    cfg = {
        "source_field": "Notes",
        "table": "Content",
        "base_id": "appX",
        "naming_pattern": "{src} ({loc})",
        "source_locale": "en-US",
    }
    cfg.update(extra_cfg or {})
    return cstore.create("airtable", name, cfg, {"api_key": "patX"}, actor="admin")


def _set_public_url():
    app_settings.set_many({"app_base_url": "https://tms.example.com"})


# ============================================================================
def test_public_url_guard():
    # unset / http / localhost all refuse with an actionable message; https public OK
    app_settings.set_many({"app_base_url": ""})
    url, err = cstore._public_base_url()
    assert not url and "app_base_url" in err, ("unset refused", url, err)
    app_settings.set_many({"app_base_url": "http://tms.example.com"})
    url, err = cstore._public_base_url()
    assert not url and "HTTPS" in err, ("http refused", url, err)
    app_settings.set_many({"app_base_url": "https://localhost:8000"})
    url, err = cstore._public_base_url()
    assert not url and "local" in err.lower(), ("localhost refused", url, err)
    app_settings.set_many({"app_base_url": "https://192.168.1.5"})
    url, err = cstore._public_base_url()
    assert not url, ("private ip refused", url, err)
    _set_public_url()
    url, err = cstore._public_base_url()
    assert url == "https://tms.example.com" and not err, ("public https OK", url, err)

    # F4 fix (review): the old string-prefix allowlist only checked 172.16.x
    # (missing 172.17-31.x), had no 169.254.x/16 link-local check (incl. the
    # cloud metadata endpoint 169.254.169.254), and no *.internal hostname
    # check. Now IP-literal hosts are gated via ipaddress.ip_address()
    # is_private/is_link_local/is_loopback/is_reserved (+ explicit 100.64.0.0/10
    # CGNAT, which is_private alone does not flag), and hostnames ending
    # .internal/.local are rejected as never-publicly-routable by convention.
    for bad in (
        "https://172.20.1.1",  # 172.17-31.x, missed by the old check
        "https://169.254.169.254",  # link-local / cloud metadata endpoint
        "https://foo.internal",
    ):  # .internal hostname convention
        app_settings.set_many({"app_base_url": bad})
        url, err = cstore._public_base_url()
        assert not url and "local/private" in err, (f"{bad} refused", url, err)
    _set_public_url()
    url, err = cstore._public_base_url()
    assert url == "https://tms.example.com" and not err, (
        "a public hostname is still accepted",
        url,
        err,
    )
    print(
        "  1b. F4: 172.17-31.x, 169.254.x (link-local/metadata), *.internal all refused; "
        "public hostname accepted"
    )

    # register refuses cleanly on a localhost url (no network call happens)
    app_settings.set_many({"app_base_url": "http://localhost:8000"})
    conn = _mk_conn("Guard")
    ok, msg = cstore.register_airtable_webhook(conn.id)
    assert not ok and "app_base_url" in msg, ("register refused on localhost", ok, msg)
    print("  1. public-url guard: unset/http/localhost/private refused; https public accepted")


def test_register_and_secret_vaulted():
    _set_public_url()
    raw_key = b"airtable-mac-raw-key-2026"
    state = {"base": "appX", "mac_b64": base64.b64encode(raw_key).decode()}
    conn = _mk_conn("Register")
    ok, msg = cstore.register_airtable_webhook(conn.id, transport=_make_transport(state))
    assert ok, msg
    # spec shape: watches the SOURCE field id, excludes publicApi
    spec = state["created"][0]["specification"]
    filt = spec["options"]["filters"]
    assert filt["watchDataInFieldIds"] == ["fldSRC"], filt
    assert "publicApi" not in filt["fromSources"] and "client" in filt["fromSources"], filt
    assert filt["recordChangeScope"] == "tblC", filt
    assert (
        state["created"][0]["notificationUrl"]
        == f"https://tms.example.com/webhooks/connectors/{conn.id}"
    ), state["created"][0]
    # secret vaulted as webhook_secret (base64), config persisted
    with Session(engine) as s:
        row = s.get(Connection, conn.id)
        sec = cstore.dec_secret(row.secret_enc)
        cfg = row.config
    assert sec.get("webhook_secret") == state["mac_b64"], ("mac secret vaulted", sec.keys())
    wh = cfg["airtable_webhook"]
    assert wh["id"] == "achNEW" and wh["source_field_id"] == "fldSRC" and wh["cursor"] == 0, wh
    assert wh["expiration_time"] == "2026-07-13T00:00:00.000Z", wh
    # the vaulted mac secret verifies a real ping through the intake
    assert webhooks.supports_webhook("airtable")
    ping = json.dumps(
        {"base": {"id": "appX"}, "webhook": {"id": "achNEW"}, "timestamp": "2026-07-06T00:00:00Z"}
    ).encode()
    mac = "hmac-sha256=" + hmac.new(raw_key, ping, hashlib.sha256).hexdigest()
    assert webhooks.verify_signature(
        "airtable", sec["webhook_secret"], ping, {"X-Airtable-Content-MAC": mac}
    ), "vaulted secret verifies ping"
    print(
        "  2. register: spec shape (watch source, exclude publicApi), mac secret vaulted + verifies"
    )


def test_ping_pull_source_change_only():
    _set_public_url()
    state = {
        "base": "appX",
        "mac_b64": base64.b64encode(b"k2").decode(),
        "records": {"recA": "Source **text** one."},
    }
    conn = _mk_conn("Pull", {"auto_translate_on_change": "1"})
    assert cstore.register_airtable_webhook(conn.id, transport=_make_transport(state))[0]
    # one page: recA's SOURCE field changed (fldSRC), recB only TARGET (fldTGT echo)
    state["payloads_pages"] = [
        _payloads_page({"recA": ["fldSRC"], "recB": ["fldTGT"]}, cursor=5, more=False)
    ]
    ok, msg, summary = cstore.consume_airtable_payloads(conn.id, transport=_make_transport(state))
    assert ok, msg
    # only recA (source changed) was translated; recB (target echo) ignored
    assert summary.get("changed") == 1, ("only source-changed record", summary)
    assert summary.get("pushed") == 1 and state["pushed"], ("recA translated + written", summary)
    assert state["pushed"][0]["id"] == "recA", state["pushed"]
    # cursor advanced to 5 and persisted
    with Session(engine) as s:
        wh = (s.get(Connection, conn.id).config or {})["airtable_webhook"]
    assert wh["cursor"] == 5, ("cursor advanced + persisted", wh)
    print(
        "  3. ping->pull: only SOURCE-changed record re-translated; target echo ignored; cursor persisted"  # noqa: E501
    )


def test_cursor_persists_across_runs():
    _set_public_url()
    state = {
        "base": "appX",
        "mac_b64": base64.b64encode(b"k3").decode(),
        "records": {"recA": "First."},
    }
    conn = _mk_conn("Cursor")
    assert cstore.register_airtable_webhook(conn.id, transport=_make_transport(state))[0]
    # run 1: cursor advances 0 -> 3
    state["payloads_pages"] = [_payloads_page({"recA": ["fldSRC"]}, cursor=3, more=False)]
    cstore.consume_airtable_payloads(conn.id, transport=_make_transport(state))
    # run 2: the stored cursor (3) is sent on the next list-payloads request
    state2 = {
        "base": "appX",
        "mac_b64": state["mac_b64"],
        "records": {},
        "payloads_pages": [{"payloads": [], "cursor": 3, "mightHaveMore": False}],
    }
    cstore.consume_airtable_payloads(conn.id, transport=_make_transport(state2))
    seen = [c for c in (state2.get("cursors_seen") or []) if c is not None]
    assert seen and seen[0] == "3", ("stored cursor sent on next run", state2.get("cursors_seen"))
    print("  4. cursor persists across runs (next fetch sends the stored cursor)")


def test_target_echo_no_translation():
    _set_public_url()
    state = {
        "base": "appX",
        "mac_b64": base64.b64encode(b"k4").decode(),
        "records": {"recA": "src"},
    }
    conn = _mk_conn("Echo")
    assert cstore.register_airtable_webhook(conn.id, transport=_make_transport(state))[0]
    # ONLY the target column changed -> no source change -> nothing translated
    state["payloads_pages"] = [_payloads_page({"recA": ["fldTGT"]}, cursor=2, more=False)]
    ok, _msg, summary = cstore.consume_airtable_payloads(conn.id, transport=_make_transport(state))
    assert ok and summary.get("changed") == 0 and not state.get("pushed"), (
        "target-echo ping -> no translation",
        summary,
        state.get("pushed"),
    )
    print("  5. target-column-only ping produces NO translation")


def test_auto_off_pending_then_on_and_force():
    _set_public_url()
    # auto OFF (default): ping records pending, translates nothing
    state = {
        "base": "appX",
        "mac_b64": base64.b64encode(b"k5").decode(),
        "records": {"recA": "hello **world**"},
    }
    conn = _mk_conn("AutoOff")  # no auto_translate_on_change -> OFF
    assert cstore.register_airtable_webhook(conn.id, transport=_make_transport(state))[0]
    state["payloads_pages"] = [_payloads_page({"recA": ["fldSRC"]}, cursor=7, more=False)]
    ok, _msg, summary = cstore.consume_airtable_payloads(conn.id, transport=_make_transport(state))
    assert ok and summary.get("pending") and summary.get("translated", 0) == 0, (
        "auto off -> pending, no translate",
        summary,
    )
    assert not state.get("pushed"), ("auto off writes nothing", state.get("pushed"))
    st = cstore.airtable_webhook_status(conn.id)
    assert st["pending_record_ids"] == ["recA"] and not st["auto_translate_on_change"], st
    print("  6a. auto_translate_on_change OFF -> pending status only, no translation")

    # force=True (Translate pending now) drains the queue: fetches newer payloads
    # (none) but translates the pending records.
    state_force = {
        "base": "appX",
        "mac_b64": state["mac_b64"],
        "records": {"recA": "hello **world**"},
        "payloads_pages": [{"payloads": [], "cursor": 7, "mightHaveMore": False}],
    }
    ok2, _msg2, summary2 = cstore.consume_airtable_payloads(
        conn.id, transport=_make_transport(state_force), force=True
    )
    assert ok2 and state_force.get("pushed"), (
        "force translates pending",
        summary2,
        state_force.get("pushed"),
    )
    assert cstore.airtable_webhook_status(conn.id)["pending_record_ids"] == [], "pending drained"
    print("  6b. force=True (translate-pending) drains the queue")

    # auto ON: ping translates immediately
    state_on = {
        "base": "appX",
        "mac_b64": base64.b64encode(b"k6").decode(),
        "records": {"recB": "content"},
    }
    conn_on = _mk_conn("AutoOn", {"auto_translate_on_change": "1"})
    assert cstore.register_airtable_webhook(conn_on.id, transport=_make_transport(state_on))[0]
    state_on["payloads_pages"] = [_payloads_page({"recB": ["fldSRC"]}, cursor=9, more=False)]
    ok3, _msg3, summary3 = cstore.consume_airtable_payloads(
        conn_on.id, transport=_make_transport(state_on)
    )
    assert ok3 and summary3.get("translated", 0) >= 1 and state_on.get("pushed"), (
        "auto on -> immediate translate",
        summary3,
        state_on.get("pushed"),
    )
    print("  6c. auto_translate_on_change ON -> targeted translation runs on the ping")


def test_keepalive_refresh_and_404_cleanup():
    _set_public_url()
    from datetime import datetime, timedelta

    state = {"base": "appX", "mac_b64": base64.b64encode(b"k7").decode()}
    conn = _mk_conn("KeepAlive")
    assert cstore.register_airtable_webhook(conn.id, transport=_make_transport(state))[0]
    # backdate refreshed_at so refresh is due
    with Session(engine) as s:
        row = s.get(Connection, conn.id)
        cfg = dict(row.config)
        wh = dict(cfg["airtable_webhook"])
        wh["refreshed_at"] = (datetime.utcnow() - timedelta(days=7)).isoformat()
        cfg["airtable_webhook"] = wh
        row.config = cfg
        s.add(row)
        s.commit()
    n, m = cstore.refresh_webhooks(conn.id, transport=_make_transport(state))
    assert n == 1, ("refresh fired", n, m)
    with Session(engine) as s:
        wh = dict(s.get(Connection, conn.id).config["airtable_webhook"])
    assert wh["expiration_time"] == "2026-07-20T00:00:00.000Z", ("expiry updated", wh)
    print("  7a. keep-alive: due webhook refreshed, expiry updated in config")

    # 404 on refresh -> registration cleared cleanly (config + vaulted secret), no error
    with Session(engine) as s:
        row = s.get(Connection, conn.id)
        cfg = dict(row.config)
        wh = dict(cfg["airtable_webhook"])
        wh["refreshed_at"] = (datetime.utcnow() - timedelta(days=7)).isoformat()
        cfg["airtable_webhook"] = wh
        row.config = cfg
        s.add(row)
        s.commit()
    state["refresh_404"] = True
    n2, m2 = cstore.refresh_webhooks(conn.id, transport=_make_transport(state))
    assert n2 == 0, (n2, m2)
    with Session(engine) as s:
        row = s.get(Connection, conn.id)
        cfg = row.config or {}
        sec = cstore.dec_secret(row.secret_enc)
    assert "airtable_webhook" not in cfg, ("registration cleared on 404", cfg)
    assert "webhook_secret" not in sec, ("vaulted mac secret cleared on 404", sec.keys())
    print("  7b. keep-alive 404 -> registration + vaulted secret cleared cleanly (no hard error)")


def test_intake_ping_enqueues_pull_job_org_scoped():
    _set_public_url()
    raw_key = b"intake-raw-key"
    state = {"base": "appX", "mac_b64": base64.b64encode(raw_key).decode()}
    conn = _mk_conn("Intake")
    assert cstore.register_airtable_webhook(conn.id, transport=_make_transport(state))[0]
    with Session(engine) as s:
        cstore.dec_secret(s.get(Connection, conn.id).secret_enc)["webhook_secret"]

    # a bare ping (no record ids) MUST enqueue a payload-pull job (ping-then-pull)
    ping = json.dumps(
        {"base": {"id": "appX"}, "webhook": {"id": "achNEW"}, "timestamp": "2026-07-06T01:00:00Z"}
    ).encode()
    mac = "hmac-sha256=" + hmac.new(raw_key, ping, hashlib.sha256).hexdigest()
    enq = []

    def _enqueue(cid, kind, record_ids):
        enq.append((cid, kind, list(record_ids)))
        return 555

    res = webhooks.handle_intake(conn.id, ping, {"X-Airtable-Content-MAC": mac}, enqueue=_enqueue)
    assert res.status == 200 and res.enqueued, ("ping enqueues a pull job", res.as_dict())
    assert enq == [(conn.id, "airtable", [])], ("empty-id pull job for THIS connection", enq)
    print("  8a. bare ping (no ids) enqueues a payload-pull job for the signed connection")

    # org scoping: a ping signed for conn's secret does not authorize another conn.
    conn2 = _mk_conn("Other")  # different connection, no webhook secret set
    res2 = webhooks.handle_intake(conn2.id, ping, {"X-Airtable-Content-MAC": mac}, enqueue=_enqueue)
    assert res2.status == 401, ("wrong-connection ping -> 401 (secret mismatch)", res2.as_dict())
    print("  8b. a ping signed for connection A is 401 against connection B (org/secret scoped)")


def _ceiling_transport(base, records, pushed, fail_ids=None):
    """3 records x 2 target-locale columns (Notes (es-MX), Notes (de-DE)). Optional
    fail_ids: PATCH batches touching any of these record ids return 500 (simulates
    a partial write-back failure for the F2 pending-retention test)."""
    fail_ids = set(fail_ids or [])

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        m = req.method
        if m == "GET" and path == f"/v0/meta/bases/{base}/tables":
            return httpx.Response(
                200,
                json={
                    "tables": [
                        {
                            "id": "tblC",
                            "name": "Content",
                            "fields": [
                                {"id": "fldSRC", "name": "Notes", "type": "singleLineText"},
                                {"id": "fldES", "name": "Notes (es-MX)", "type": "singleLineText"},
                                {"id": "fldDE", "name": "Notes (de-DE)", "type": "singleLineText"},
                            ],
                        }
                    ]
                },
            )
        if m == "GET" and path.startswith(f"/v0/{base}/"):
            recs = [{"id": rid, "fields": {"Notes": txt}} for rid, txt in records.items()]
            return httpx.Response(200, json={"records": recs})
        if m == "PATCH" and path.startswith(f"/v0/{base}/"):
            body = json.loads(req.content)
            ids = {r["id"] for r in body["records"]}
            if ids & fail_ids:
                return httpx.Response(500, text="simulated write failure")
            pushed.extend(body["records"])
            return httpx.Response(200, json={"records": body["records"]})
        return httpx.Response(404, json={"path": path, "method": m})

    return httpx.MockTransport(handler)


def test_f1_write_ceiling_caps_units_and_leaves_remainder():
    """F1 (MONEY finding, review): dynamic_translate had no write ceiling at all --
    a bulk paste x N locale columns could fire thousands of uncapped MT calls in
    one wave. With MAX_WRITES_PER_RUN patched to 4 and 3 records x 2 locale
    columns pending (6 possible record x locale units), exactly 4 units must be
    translated/written, a clear ceiling message must be present, and the
    un-drained remainder must be reported so a later run picks it up."""
    _set_public_url()
    conn = _mk_conn("Ceiling", {"naming_pattern": "{src} ({loc})", "base_id": "appCeil"})  # #419
    records = {"recA": "Alpha text.", "recB": "Beta text.", "recC": "Gamma text."}
    pushed = []
    real_ceiling = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 4
    try:
        ok, msg, summary = cstore.dynamic_translate(
            conn.id,
            base_id="appCeil",
            table="Content",
            transport=_ceiling_transport("appCeil", records, pushed),
        )
    finally:
        app_config.MAX_WRITES_PER_RUN = real_ceiling
    assert ok, msg
    assert summary["ceiling_hit"] is True and summary["write_ceiling"] == 4, summary
    assert summary["translated"] == 4, ("exactly 4 unit writes, not 6", summary)
    assert "⚠" in msg and "ceiling" in msg.lower(), ("loud ceiling message", msg)
    # some record(s) were capped out entirely (never got a unit built this run)
    assert summary["capped_record_ids"], ("capped-out records reported", summary)
    print(
        "  9a. F1: ceiling of 4 with 3 records x 2 locales pending -> exactly 4 unit "
        "writes + a clear ceiling message + reported remainder"
    )


def test_f2_partial_failure_and_capped_records_stay_pending():
    """F2 (review): consume_airtable_payloads used to clear ALL changed ids from
    airtable_pending_records whenever dynamic_translate returned ok=True (ok only
    means >=1 record wrote) -- a partial failure silently vanished from the queue.
    2 records pending, one (recFail) whose write-back fails: recFail must still be
    pending after the run; the succeeded one must be removed.

    patch_records batches PATCHes in 10s, so a real single-batch HTTP failure
    would fail BOTH records together, not just one -- a genuine one-fails-one-
    succeeds outcome needs per-record granularity that only the connector's
    return value expresses (real Airtable batch PATCHes are all-or-nothing per
    batch too). Monkeypatch AirtableConnector.patch_records (the same fault-
    injection technique verify_airtable_p1 uses for _mock_translate) to return
    exactly that shape, so the test targets consume_airtable_payloads' pending-
    retention logic directly rather than incidental batch-size mechanics."""
    _set_public_url()
    state = {
        "base": "appX",
        "mac_b64": base64.b64encode(b"k8").decode(),
        "records": {"recOk": "Fine text.", "recFail": "Bad text."},
    }
    # auto OFF (default): register, then seed BOTH records as already-pending —
    # exactly the state an earlier auto-off ping would have left (_add_pending_
    # records), so this run drains the backlog via force=True rather than
    # freshly-changed ids from a payload page.
    conn = _mk_conn("Partial")
    assert cstore.register_airtable_webhook(conn.id, transport=_make_transport(state))[0]
    cstore._add_pending_records(conn.id, ["recOk", "recFail"])
    state["payloads_pages"] = [{"payloads": [], "cursor": 0, "mightHaveMore": False}]

    real_patch_records = at.AirtableConnector.patch_records
    pushed = {}

    def _fake_patch_records(self, base, table, updates, typecast=False):
        pushed.update(updates)
        ok_updates = {rid: flds for rid, flds in updates.items() if rid != "recFail"}
        failed = [rid for rid in updates if rid == "recFail"]
        n = len(ok_updates)
        msg = (
            f"Updated {n} record(s); {len(failed)} failed. simulated failure"
            if failed
            else f"Updated {n} record(s)."
        )
        return n, failed, msg

    at.AirtableConnector.patch_records = _fake_patch_records
    try:
        ok, msg, summary = cstore.consume_airtable_payloads(
            conn.id, transport=_make_transport(state), force=True
        )
    finally:
        at.AirtableConnector.patch_records = real_patch_records
    assert ok, (
        msg
    )  # recOk wrote, so ok=True overall (the OLD bug: this used to wipe BOTH from pending)
    failed_ids = {f.get("record_id") for f in (summary.get("failures") or [])}
    assert "recFail" in failed_ids, ("recFail surfaced as a structured failure", summary)
    st = cstore.airtable_webhook_status(conn.id)
    assert st["pending_record_ids"] == ["recFail"], (
        "failed record stays pending; succeeded one removed",
        st,
    )
    assert "recOk" in pushed, ("recOk was attempted/written", pushed)
    print(
        "  9b. F2: 2 records pending, one fails -> failed record still pending, "
        "succeeded one removed"
    )


def test_f3_ping_pull_coalesces_to_one_job():
    """F3 (review): N distinct-timestamp pings before the worker drains the first
    one should enqueue exactly ONE payload-pull job for that connection, not N --
    each ping-pull payload carries no per-event data (always record_ids=[]; the
    real content comes from the stored cursor at run time), so coalescing loses
    nothing. Seed a QUEUED connector_translate/ping-pull job directly (bypassing
    the real worker queue, mirroring verify_semantic_tm's technique) so nothing
    drains it during the test, then fire 5 signed pings through the real HTTP
    route and confirm they all re-attach to the SAME job id."""
    _set_public_url()
    raw_key = b"coalesce-raw-key"
    state = {"base": "appX", "mac_b64": base64.b64encode(raw_key).decode()}
    conn = _mk_conn("Coalesce")
    assert cstore.register_airtable_webhook(conn.id, transport=_make_transport(state))[0]
    with Session(engine) as s:
        cstore.dec_secret(s.get(Connection, conn.id).secret_enc)["webhook_secret"]

    # drop any pre-existing connector_translate jobs so this connection's active
    # set is exactly the one we seed below (mirrors verify_semantic_tm's approach).
    with Session(engine) as s:
        for j in s.exec(select(Job).where(Job.kind == "connector_translate")).all():
            if (j.payload or {}).get("connection_id") == conn.id:
                s.delete(j)
        s.commit()
        seeded = Job(
            kind="connector_translate",
            status=JobStatus.QUEUED,
            payload={
                "connection_id": conn.id,
                "actor": "webhook",
                "webhook_record_ids": [],
                "webhook_article_ids": [],
                "webhook_ping_pull": True,
            },
            created_by="webhook",
        )
        s.add(seeded)
        s.commit()
        s.refresh(seeded)
        seeded_id = seeded.id
    # NOTE: this row was inserted directly (not via jobs.submit_unique/_queue.put),
    # so no real worker thread has been handed it -- it stays QUEUED and inert
    # until something calls submit_unique/submit again, exactly like the
    # semantic-tm backfill-debounce test's seeded-row technique.

    c = TestClient(app)
    job_ids = set()
    for i in range(5):
        ping = json.dumps(
            {
                "base": {"id": "appX"},
                "webhook": {"id": "achNEW"},
                "timestamp": f"2026-07-06T02:0{i}:00Z",
            }
        ).encode()
        mac = "hmac-sha256=" + hmac.new(raw_key, ping, hashlib.sha256).hexdigest()
        r = c.post(
            f"/webhooks/connectors/{conn.id}",
            content=ping,
            headers={"X-Airtable-Content-MAC": mac, "Content-Type": "application/json"},
        )
        assert r.status_code == 200 and r.json().get("enqueued"), r.text
        job_ids.add(r.json()["job_id"])

    assert job_ids == {seeded_id}, (
        "5 distinct-timestamp pings before the worker runs -> 1 pull job "
        "(coalesced onto the seeded QUEUED job)",
        job_ids,
        seeded_id,
    )
    with Session(engine) as s:
        rows = [
            j
            for j in s.exec(select(Job).where(Job.kind == "connector_translate")).all()
            if (j.payload or {}).get("connection_id") == conn.id
        ]
    assert len(rows) == 1 and rows[0].id == seeded_id, (
        "exactly one connector_translate job row for this connection",
        [r.id for r in rows],
    )
    print(
        "  9c. F3: 5 distinct-timestamp pings before the worker runs -> 1 pull job "
        "(submit_unique coalesces on connection_id)"
    )


def main():
    init_db()
    test_public_url_guard()
    test_register_and_secret_vaulted()
    test_ping_pull_source_change_only()
    test_cursor_persists_across_runs()
    test_target_echo_no_translation()
    test_auto_off_pending_then_on_and_force()
    test_keepalive_refresh_and_404_cleanup()
    test_intake_ping_enqueues_pull_job_org_scoped()
    test_f1_write_ceiling_caps_units_and_leaves_remainder()
    test_f2_partial_failure_and_capped_records_stay_pending()
    test_f3_ping_pull_coalesces_to_one_job()
    print("verify_airtable_p2 OK")


if __name__ == "__main__":
    main()
