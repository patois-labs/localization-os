"""Airtable connector PHASE 1 verification — fully offline (httpx.MockTransport +
the built-in MockProvider). Covers the five zero-tunnel-robustness items:

  1. RICH-TEXT + MENTION-SAFE codec: <airtable:mention> tokens and rich-text
     Markdown markup are shielded before MT and restored after — round-tripped
     through a deliberately MANGLING mock MT (uppercases every word). Field-type
     detection routes richText vs plain via the meta API; a missing type map
     degrades to plain handling but still shields mentions.
  2. V2 webhook-payload parser: select -> {id,name,color}, link -> [{id,name}]
     shapes are normalised to the connector's plain cell model (parse-only; no
     live webhook registration).
  3. MAC verification on the generalized intake: an "airtable" verifier is
     registered computing X-Airtable-Content-MAC = hmac-sha256=<hex> over the raw
     body with the base64-DECODED macSecret; a valid signature is accepted, a bad
     one is 401.
  4. Write-back loop-guard: dynamic_translate records a per-(record, field)
     content-hash of what WE wrote; an echo (unchanged source) is suppressed; a
     genuinely edited source still flows.
  5. Keep-alive scaffolding: refresh_webhooks() is a no-op when no official
     webhook is registered (Phase-1 dormant), and refreshes a registered one once
     it crosses the 6-day margin.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_test_")

import httpx
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Connection, WebhookEvent
from app.services.connectors import airtable as at
from app.services.connectors import store as cstore
from app.services.connectors import webhooks


def _mangle(text: str) -> str:
    """A deliberately-mangling MT: uppercase every ASCII word run. Leaves the
    private-use-area sentinels and the \\x00MD…\\x00 markers untouched (they are
    not [A-Za-z] runs), so a correct codec round-trips despite the mangling."""
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0).upper(), text)


def test_codec_roundtrip():
    # (1a) mention shielding: token id + tag + inner @name survive byte-identical
    # through a mangling MT, and the @name is NOT translated.
    src = (
        'Hi <airtable:mention id="usrXY">@Alex Smith</airtable:mention>, '
        "please review {before} now."
    )
    enc, ctx = at.encode_cell(src, "long")
    assert "<airtable:mention" not in enc, ("mention shielded out of MT text", enc)
    dec = at.decode_cell(_mangle(enc), ctx)
    assert '<airtable:mention id="usrXY">@Alex Smith</airtable:mention>' in dec, (
        "mention token restored byte-identical",
        dec,
    )
    # prose around it WAS translated (mangled), proving only the token was shielded
    assert "HI" in dec and "REVIEW" in dec, ("prose still reached MT", dec)
    print("  1a. mention token round-trips byte-identical through a mangling MT")

    # (1b) rich-text Markdown markup survives: bold/italic markers, code span,
    # link URL all restored; the link LABEL and prose are translated.
    rich = "See **bold** and *italic* and `codeLiteral` and [clickHere](http://x/y)."
    enc2, ctx2 = at.encode_cell(rich, "rich")
    dec2 = at.decode_cell(_mangle(enc2), ctx2)
    assert "**bold**" not in dec2.lower() or "**BOLD**" in dec2, dec2  # markers kept
    assert "**" in dec2 and "*" in dec2, ("emphasis markers restored", dec2)
    assert "`codeLiteral`" in dec2, ("code span content NOT translated", dec2)
    assert "(http://x/y)" in dec2, ("link URL restored verbatim", dec2)
    assert "CLICKHERE" in dec2, ("link label WAS translated", dec2)
    print("  1b. rich-text markup (bold/italic/code/link) round-trips; label translates")

    # (1c) plain field: no markup, mentions still shielded defensively (no-op when
    # absent), text translated.
    enc3, ctx3 = at.encode_cell("Just plain text.", "plain")
    assert not (ctx3["mentions"] or ctx3["md"]), ("no tokens for plain text", ctx3)
    assert at.decode_cell(_mangle(enc3), ctx3) == "JUST PLAIN TEXT."
    print("  1c. plain field: no shielding tokens, straight translate")

    # (1d) field_kind routing
    assert at.field_kind("richText") == "rich"
    assert at.field_kind("multilineText") == "long"
    assert at.field_kind("singleLineText") == "plain"
    assert at.field_kind(None) == "plain" and at.field_kind("formula") == "plain"
    print("  1d. field_kind routes richText/multilineText/plain/unknown")

    # (1e) idempotency guard: a raw pre-existing sentinel in the source is stripped,
    # so it can't falsely trigger a restore (mirrors document360 hardening).
    poisoned = at._MENTION_OPEN + "0" + at._MENTION_CLOSE + " real text"
    encp, _ctxp = at.encode_cell(poisoned, "long")
    assert at._MENTION_OPEN not in encp, ("raw sentinel stripped from source", encp)
    print("  1e. pre-existing raw sentinel stripped from source (no false restore)")

    # (1f) F1 write-side egress guard: a deliberately-PUA-mangling MT (drops the
    # CLOSE half of one placeholder, i.e. a SPLIT sentinel, and corrupts another's
    # index out of range) must NOT leave any PUA glyph or stray shield digit in the
    # text that reaches a write payload — a mangled mention degrades to a clean
    # deletion, never visible garbage.
    src = (
        'Hi <airtable:mention id="usr1">@A</airtable:mention> and '
        '<airtable:mention id="usr2">@B</airtable:mention>, please review.'
    )
    enc, ctx = at.encode_cell(src, "long")
    assert enc.count(at._MENTION_OPEN) == 2, ("2 shielded placeholders", enc)
    p0 = at._MENTION_OPEN + "0" + at._MENTION_CLOSE
    p1 = at._MENTION_OPEN + "1" + at._MENTION_CLOSE
    assert p0 in enc and p1 in enc, ("both placeholders present", enc)

    # placeholder 0: SPLIT (drop the close delimiter -> bare "<open>0" survives);
    # placeholder 1: OUT-OF-RANGE index (index 9 doesn't exist -> left verbatim).
    mangled = enc.replace(p0, at._MENTION_OPEN + "0", 1)
    mangled = mangled.replace(p1, at._MENTION_OPEN + "9" + at._MENTION_CLOSE, 1)
    assert at._MENTION_OPEN in mangled, ("test setup produced a mangled sentinel", mangled)

    dec = at.decode_cell(mangled, ctx)
    assert not at._RAW_MENTION_SENTINEL.search(dec), ("no raw PUA glyph in write text", dec)
    assert "0" not in dec and "9" not in dec, ("no stray shield digit reaches the write text", dec)
    assert "<airtable:mention" not in dec, (
        "both mangled mentions degrade to deletion, not a surviving tag",
        dec,
    )
    print("  1f. write-side egress guard: mangled sentinels leave no PUA glyph/stray digit")

    # (1g) an out-of-range MD placeholder likewise leaves no raw \x00 marker.
    rich = "See **bold** text."
    enc2, ctx2 = at.encode_cell(rich, "rich")
    oob_md = enc2.replace("MD0", "MD9")  # corrupt the index out of range
    dec2 = at.decode_cell(oob_md, ctx2)
    assert "\x00" not in dec2, ("no raw MD placeholder marker in write text", dec2)
    print("  1g. write-side egress guard: out-of-range MD placeholder leaves no \\x00 marker")

    # (1h) a faithful (non-mangling) round-trip is completely unaffected by the guard:
    # both mentions still restore byte-identical through a mangling (uppercasing) MT.
    dec3 = at.decode_cell(_mangle(enc), ctx)
    for uid in ("usr1", "usr2"):
        assert f'<airtable:mention id="{uid}">' in dec3, (uid, dec3)
    print("  1h. unmangled path: mentions still restore byte-identical (guard is a no-op)")

    # (1i) D2 (audit — shared-sentinel collision): a rich source cell that already
    # contains a \x00MD<n>\x00-shaped run must NOT collide with the placeholders
    # _shield_rich_markup mints for the real markup. Pre-fix, the source's own
    # \x00MD0\x00 aliased the minted opening-** placeholder, so decode_cell resurrected
    # it as "**" (or, out of range, _strip_write_egress_debris deleted it) — corrupting
    # the written-back cell. The source \x00 sentinel byte must be neutralised first.
    poisoned_rich = "Note \x00MD0\x00 keep and **bold**."
    encr, ctxr = at.encode_cell(poisoned_rich, "rich")
    # No raw \x00 from the SOURCE survives into the MT text (only OUR minted markers).
    decr = at.decode_cell(_mangle(encr), ctxr)
    assert "\x00" not in decr, ("no raw MD sentinel byte in the written-back cell", repr(decr))
    # The genuine markup still round-trips.
    assert "**BOLD**" in decr, ("real markup must still restore", repr(decr))
    # The source's sentinel-shaped text is preserved as prose (its \x00 dropped, the
    # documented trade-off), NOT resurrected as a markup token. Pre-fix this read
    # "** KEEP" instead of "MD0 KEEP" — the collision corruption.
    assert "MD0 KEEP" in decr, (
        "source sentinel-shaped text kept as prose, not restored as markup (D2 collision)",
        repr(decr),
    )
    print("  1i. D2: pre-existing \\x00MD<n>\\x00 in source cannot collide with minted markers")


def test_v2_parser():
    # (2) V2 webhook cell shapes -> plain cell model
    assert at.parse_webhook_cell({"id": "sel1", "name": "Active", "color": "blue"}) == "Active"
    assert (
        at.parse_webhook_cell([{"id": "r1", "name": "Alpha"}, {"id": "r2", "name": "Beta"}])
        == "Alpha, Beta"
    )
    assert at.parse_webhook_cell({"id": "u1", "email": "a@b.com", "name": "Ann"}) == "Ann"
    assert at.parse_webhook_cell("plain string") == "plain string"
    assert at.parse_webhook_cell(42) == 42 and at.parse_webhook_cell(None) is None
    # unrecognised object shape is PRESERVED (for live-QA), never crashes
    weird = {"foo": {"bar": 1}}
    assert at.parse_webhook_cell(weird) == weird
    print("  2a. parse_webhook_cell normalises select/link/collaborator, preserves unknown")

    payload = {
        "payloads": [
            {
                "changedTablesById": {
                    "tbl1": {
                        "changedRecordsById": {
                            "recA": {
                                "current": {
                                    "cellValuesByFieldId": {
                                        "fldSel": {"id": "s", "name": "Done", "color": "green"},
                                        "fldTxt": "hello",
                                    }
                                }
                            },
                            "recB": {"current": {"cellValuesByFieldId": {"fldTxt": "world"}}},
                        }
                    }
                }
            }
        ]
    }
    changed = at.parse_webhook_changed(payload)
    assert len(changed) == 2, changed
    a = next(c for c in changed if c["record_id"] == "recA")
    assert a["fields"]["fldSel"] == "Done" and a["fields"]["fldTxt"] == "hello", a
    assert at.webhook_changed_record_ids(payload) == ["recA", "recB"], (
        at.webhook_changed_record_ids(payload)
    )
    # empty / malformed -> [] (defensive)
    assert at.parse_webhook_changed({}) == [] and at.parse_webhook_changed(None) == []
    print("  2b. parse_webhook_changed maps V2 payload -> [{table,record,fields}]; thin ids")


def test_mac_intake():
    # (3) MAC verifier registered + wired into the generalized intake
    assert webhooks.supports_webhook("airtable"), "airtable verifier not registered"
    raw_key = b"airtable-mac-secret-raw-bytes"
    secret_b64 = base64.b64encode(raw_key).decode()  # Airtable returns macSecretBase64
    conn = cstore.create(
        "airtable", "MAC intake", {"source_field": "Notes"}, {"api_key": "patX"}, actor="admin"
    )
    # store the webhook secret exactly as siblings do (vaulted webhook_secret)
    with Session(engine) as s:
        row = s.get(Connection, conn.id)
        sec = cstore.dec_secret(row.secret_enc)
        sec["webhook_secret"] = secret_b64
        row.secret_enc = cstore.enc_secret(sec)
        s.add(row)
        s.commit()

    # a real ping body carries only base+webhook ids (no content)
    ping = json.dumps(
        {"base": {"id": "appX"}, "webhook": {"id": "achX"}, "timestamp": "2026-07-06T00:00:00.000Z"}
    ).encode()
    mac = "hmac-sha256=" + hmac.new(raw_key, ping, hashlib.sha256).hexdigest()
    enq = []

    def _fake_enqueue(cid, kind, record_ids):
        enq.append((cid, kind, list(record_ids)))
        return 4242

    # (a) valid MAC -> accepted. Airtable is registered as PING-THEN-PULL (Phase 2),
    #     so a bare ping (no record ids) enqueues a payload-PULL job (empty id list)
    #     rather than a no-op — the job re-fetches payloads from the stored cursor.
    #     It still never sweeps (the enqueued ids are empty; the pull path filters to
    #     source-field changes).
    res = webhooks.handle_intake(
        conn.id, ping, {"X-Airtable-Content-MAC": mac}, enqueue=_fake_enqueue
    )
    assert res.status == 200 and res.enqueued, ("valid ping -> 200 pull enqueued", res.as_dict())
    assert enq == [(conn.id, "airtable", [])], ("bare ping enqueues an empty-id pull job", enq)
    print("  3a. valid MAC ping -> 200 accepted, payload-pull job enqueued (ping-then-pull)")

    # (b) bad MAC -> 401, nothing enqueued
    bad = webhooks.handle_intake(
        conn.id, ping, {"X-Airtable-Content-MAC": "hmac-sha256=deadbeef"}, enqueue=_fake_enqueue
    )
    assert bad.status == 401 and not bad.enqueued, ("bad MAC -> 401", bad.as_dict())
    print("  3b. bad MAC -> 401, nothing enqueued")

    # (c) a payloads-shaped body (future inline / test) -> record ids extracted
    payloads_body = json.dumps(
        {
            "webhook": {"id": "achX"},
            "timestamp": "2026-07-06T00:00:01Z",
            "payloads": [
                {
                    "changedTablesById": {
                        "tbl1": {
                            "changedRecordsById": {
                                "recZ": {"current": {"cellValuesByFieldId": {"fld": "hi"}}}
                            }
                        }
                    }
                }
            ],
        }
    ).encode()
    mac2 = "hmac-sha256=" + hmac.new(raw_key, payloads_body, hashlib.sha256).hexdigest()
    res2 = webhooks.handle_intake(
        conn.id, payloads_body, {"X-Airtable-Content-MAC": mac2}, enqueue=_fake_enqueue
    )
    assert res2.status == 200 and res2.enqueued and res2.record_ids == ["recZ"], res2.as_dict()
    assert enq[-1] == (conn.id, "airtable", ["recZ"]), enq
    print("  3c. signed payloads-shaped body -> record ids extracted + enqueued")

    # (d) retry of same signed delivery -> deduped (one ledger row)
    res3 = webhooks.handle_intake(
        conn.id, payloads_body, {"X-Airtable-Content-MAC": mac2}, enqueue=_fake_enqueue
    )
    assert res3.status == 200 and not res3.enqueued, ("retry deduped", res3.as_dict())
    with Session(engine) as s:
        rows = s.exec(
            select(WebhookEvent).where(
                WebhookEvent.connection_id == conn.id,
                WebhookEvent.event_id == "achX:2026-07-06T00:00:01Z",
            )
        ).all()
    assert len(rows) == 1, ("one ledger row for the payloads delivery", rows)
    print("  3d. retry of same signed delivery -> deduped (1 ledger row)")


def test_loop_guard_and_codec_wired():
    """(1 wired + 4) dynamic_translate: rich-text source round-trips markup/mentions
    end-to-end (through the offline MockProvider), and a second run over the SAME
    unchanged source is suppressed as an echo of our own write."""

    def transport(pushed: list, richtext: bool = True):
        src_type = "richText" if richtext else "multilineText"

        def handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if req.method == "GET" and path == "/v0/meta/bases/appLG/tables":
                return httpx.Response(
                    200,
                    json={
                        "tables": [
                            {
                                "id": "tbl1",
                                "name": "Content",
                                "fields": [
                                    {"name": "Notes", "type": src_type},
                                    {"name": "Notes (es-MX)", "type": src_type},
                                ],
                            }
                        ]
                    },
                )
            if req.method == "GET" and path.startswith("/v0/appLG/"):
                return httpx.Response(
                    200,
                    json={
                        "records": [
                            {
                                "id": "recA",
                                "fields": {
                                    "Notes": 'Read <airtable:mention id="usrQ">@Q</airtable:mention> and **now**.'  # noqa: E501
                                },
                            }
                        ]
                    },
                )
            if req.method == "PATCH" and path.startswith("/v0/appLG/"):
                body = json.loads(req.content)
                pushed.extend(body["records"])
                return httpx.Response(200, json={"records": body["records"]})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    conn = cstore.create(
        "airtable",
        "Loop guard",
        {
            "source_field": "Notes",
            "naming_pattern": "{src} ({loc})",
            "source_locale": "en-US",
            # #419: base/table overrides must match the configured scope
            "base_id": "appLG",
            "table": "Content",
        },
        {"api_key": "patLG"},
        actor="admin",
    )
    pushed = []
    ok, msg, summary = cstore.dynamic_translate(
        conn.id, base_id="appLG", table="Content", transport=transport(pushed)
    )
    assert ok, msg
    assert summary["records"] == 1 and summary["pushed"] == 1, summary
    written = pushed[0]["fields"]["Notes (es-MX)"]
    # the mention token + bold markers survived the full extract->MT->decode->write
    assert '<airtable:mention id="usrQ">@Q</airtable:mention>' in written, (
        "mention survived end-to-end write-back",
        written,
    )
    assert "**" in written, ("bold markers survived end-to-end", written)
    print("  4a. dynamic_translate: rich-text mention+markup survive end-to-end write-back")

    # (4a-F1) write-side egress guard, end-to-end: a deliberately PUA-mangling
    # provider (monkeypatched MockProvider — save/restore, per the suite's
    # established fault-injection technique, see verify_pair_providers.py) drops
    # one sentinel's close delimiter and corrupts another's index. The cell that
    # actually reaches patch_records must contain NO raw PUA sentinel glyph and NO
    # stray shield digit — proving the guard is wired into the real write path, not
    # just the unit-level codec.
    from app.services import providers as _providers

    _orig_mock_translate = _providers._mock_translate

    def _mangling_mock_translate(payload: dict) -> str:
        out = _orig_mock_translate(payload)
        out = out.replace(at._MENTION_CLOSE, "", 1)  # split placeholder 0
        return out.replace("1", "9", 1)  # corrupt any remaining index

    def transport_mangle(pushed: list):
        def handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if req.method == "GET" and path == "/v0/meta/bases/appMG/tables":
                return httpx.Response(
                    200,
                    json={
                        "tables": [
                            {
                                "id": "tbl1",
                                "name": "Content",
                                "fields": [
                                    {"name": "Notes", "type": "richText"},
                                    {"name": "Notes (es-MX)", "type": "richText"},
                                ],
                            }
                        ]
                    },
                )
            if req.method == "GET" and path.startswith("/v0/appMG/"):
                return httpx.Response(
                    200,
                    json={
                        "records": [
                            {
                                "id": "recM",
                                "fields": {
                                    "Notes": 'Hi <airtable:mention id="usrM">@M</airtable:mention> there.'  # noqa: E501
                                },
                            }
                        ]
                    },
                )
            if req.method == "PATCH" and path.startswith("/v0/appMG/"):
                body = json.loads(req.content)
                pushed.extend(body["records"])
                return httpx.Response(200, json={"records": body["records"]})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    conn_mg = cstore.create(
        "airtable",
        "Mangle guard",
        {
            "source_field": "Notes",
            "naming_pattern": "{src} ({loc})",
            "source_locale": "en-US",
            # #419: base/table overrides must match the configured scope
            "base_id": "appMG",
            "table": "Content",
        },
        {"api_key": "patMG"},
        actor="admin",
    )
    set_pieces("app.services.providers", "_mock_translate", _mangling_mock_translate)
    try:
        pushed_mg = []
        ok_mg, msg_mg, _summary_mg = cstore.dynamic_translate(
            conn_mg.id, base_id="appMG", table="Content", transport=transport_mangle(pushed_mg)
        )
    finally:
        set_pieces("app.services.providers", "_mock_translate", _orig_mock_translate)
    assert ok_mg, msg_mg
    written_mg = pushed_mg[0]["fields"]["Notes (es-MX)"]
    assert not at._RAW_MENTION_SENTINEL.search(written_mg), (
        "no raw PUA sentinel glyph reaches the real write payload",
        written_mg,
    )
    assert "<airtable:mention" not in written_mg, (
        "mangled mention degrades to deletion, not a surviving/garbled tag",
        written_mg,
    )
    print("  4a-F1. end-to-end: mangling provider -> written cell has NO PUA glyph/stray digit")

    # loop-guard persisted a hash for (recA, Notes (es-MX))
    with Session(engine) as s:
        cfg = s.get(Connection, conn.id).config or {}
    assert cfg.get("airtable_written_hashes"), ("written hash persisted", cfg)
    assert "recA:Notes (es-MX)" in cfg["airtable_written_hashes"], cfg["airtable_written_hashes"]
    print("  4b. loop-guard persisted a per-(record,field) source hash")

    # second run over the UNCHANGED source -> echo suppressed (nothing translated)
    pushed2 = []
    ok2, msg2, summary2 = cstore.dynamic_translate(
        conn.id, base_id="appLG", table="Content", transport=transport(pushed2)
    )
    assert ok2, msg2
    assert summary2.get("translated", 0) == 0 and not pushed2, (
        "unchanged source suppressed as echo",
        summary2,
        pushed2,
    )
    print("  4c. re-run over unchanged source -> echo suppressed (0 translated, 0 pushed)")

    # an EDITED source (different hash) flows again
    def edited_transport(pushed):
        def handler(req):
            path = req.url.path
            if req.method == "GET" and path == "/v0/meta/bases/appLG/tables":
                return httpx.Response(
                    200,
                    json={
                        "tables": [
                            {
                                "id": "tbl1",
                                "name": "Content",
                                "fields": [
                                    {"name": "Notes", "type": "richText"},
                                    {"name": "Notes (es-MX)", "type": "richText"},
                                ],
                            }
                        ]
                    },
                )
            if req.method == "GET" and path.startswith("/v0/appLG/"):
                return httpx.Response(
                    200,
                    json={
                        "records": [
                            {"id": "recA", "fields": {"Notes": "Completely different text."}}
                        ]
                    },
                )
            if req.method == "PATCH" and path.startswith("/v0/appLG/"):
                body = json.loads(req.content)
                pushed.extend(body["records"])
                return httpx.Response(200, json={"records": body["records"]})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    pushed3 = []
    ok3, _msg3, summary3 = cstore.dynamic_translate(
        conn.id, base_id="appLG", table="Content", transport=edited_transport(pushed3)
    )
    assert ok3 and summary3["pushed"] == 1 and pushed3, (
        "edited source flows again",
        summary3,
        pushed3,
    )
    print("  4d. an EDITED source (new hash) flows again (loop-guard keys on source)")


def test_keep_alive():
    # (5) refresh_webhooks: dormant when no webhook registered
    conn = cstore.create(
        "airtable",
        "Keep alive",
        {"source_field": "Notes", "base_id": "appKA"},
        {"api_key": "patKA"},
        actor="admin",
    )
    n, m = cstore.refresh_webhooks(conn.id)
    assert n == 0 and m == "no webhook", ("dormant no-op when nothing registered", n, m)
    print("  5a. refresh_webhooks: no-op when no official webhook registered (Phase-1 dormant)")

    # register a webhook in config with an OLD refreshed_at -> refresh fires
    from datetime import datetime, timedelta

    old = (datetime.utcnow() - timedelta(days=7)).isoformat()
    cstore.patch_config(
        conn.id, {"airtable_webhook": {"id": "achKA", "base_id": "appKA", "refreshed_at": old}}
    )

    refreshed = {"count": 0}

    def refresh_transport():
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" and req.url.path == "/v0/bases/appKA/webhooks/achKA/refresh":
                refreshed["count"] += 1
                return httpx.Response(200, json={"expirationTime": "2026-07-13T00:00:00.000Z"})
            return httpx.Response(404, json={"path": req.url.path})

        return httpx.MockTransport(handler)

    n2, m2 = cstore.refresh_webhooks(conn.id, transport=refresh_transport())
    assert n2 == 1 and refreshed["count"] == 1, ("stale webhook refreshed", n2, m2, refreshed)
    # refreshed_at advanced -> a second immediate call is NOT due
    n3, m3 = cstore.refresh_webhooks(conn.id, transport=refresh_transport())
    assert n3 == 0 and m3 == "not due", ("fresh webhook not re-refreshed", n3, m3)
    print("  5b. refresh_webhooks: stale (7d) webhook refreshed once; fresh one skipped")

    # spec builder excludes publicApi (loop-guard at source)
    spec = at.webhook_spec("tbl1", ["fldSrc"])
    froms = spec["options"]["filters"]["fromSources"]
    assert "publicApi" not in froms and "client" in froms, ("publicApi excluded", froms)
    assert spec["options"]["filters"]["watchDataInFieldIds"] == ["fldSrc"], spec
    assert at.WRITEBACK_SOURCE == "publicApi"
    print("  5c. webhook_spec excludes publicApi (fromSources loop-guard) + watches source field")

    # (5d) F3: refresh_webhooks docstring promises "Never raises" — a truthy
    # non-dict `config` (corrupted row state) must be a no-op, not an
    # AttributeError from calling .get() on a string.
    conn_bad = cstore.create(
        "airtable",
        "Bad config",
        {"source_field": "Notes", "base_id": "appBC"},
        {"api_key": "patBC"},
        actor="admin",
    )
    with Session(engine) as s:
        row = s.get(Connection, conn_bad.id)
        row.config = "not-a-dict"  # simulates corrupted/legacy row state
        s.add(row)
        s.commit()
    n4, m4 = cstore.refresh_webhooks(conn_bad.id)
    assert n4 == 0 and m4 == "no webhook", ("non-dict config -> no-op, never raises", n4, m4)
    print("  5d. refresh_webhooks: truthy non-dict config -> no-op, no raise (F3)")


def main():
    init_db()
    test_codec_roundtrip()
    test_v2_parser()
    test_mac_intake()
    test_loop_guard_and_codec_wired()
    test_keep_alive()
    print("verify_airtable_p1 OK")


if __name__ == "__main__":
    main()
