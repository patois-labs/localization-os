"""Salesforce Lightning Knowledge connector + the per-record-type field-mapping
generalizer (fieldmap.py) — fully offline via httpx.MockTransport (a fake org).

Salesforce is the FIELD-MAPPING GENERALIZER build (T2 SubResourceStrategy over the
shared translate-flow engine): translatable fields VARY per record type and are
discovered via Describe, so extraction + write-payload reassembly route through the
reusable fieldmap.py contract rather than a fixed title+body.

Covers:
  - generalizer (fieldmap.py): select_translatable_fields buckets by type; a
    renamed/custom field is discovered; a formula/calculated + a non-content
    (UrlName) + a non-text field are excluded; rich-text -> html; extract_record +
    Extraction.build_payload round-trip (html + plain fields, unknown record type
    fallback, missing fields)
  - auth: JWT-bearer token mint (signed RS256) + instance_url trust; test()
  - discovery: record-type containers; discover_articles (SOQL masters +
    KnowledgeArticleId scope); rediscover-fields persists sf_field_map
  - locale mapping: BCP-47 <-> SF code round-trip; active-language rejection
  - full salesforce_translate flow: CREATE (submit->PATCH->complete, draft),
    UPDATE (reuse existing Draft), publish toggle, content-hash suppression,
    empty-article skip, mid-lifecycle failure (step 3) leaves Draft + no publish,
    WriteBudget ceiling, NEEDS_REVIEW exclusion, all-rejected FAIL
  - engine-consumer regression: NO watermark key ever written (poll-first)
  - org isolation: a foreign cid -> 404 on the /api translate route
  - CLI salesforce-translate + salesforce-rediscover-fields smoke
  - no-watermark / no-mock-egress-to-real regression posture

ASCII-only prints; final line is 'verify_salesforce OK'.
"""

import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_sf_")

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Connection, Document, Segment, SegStatus
from app.services import connectors as _reg  # archive-tier spec introspection
from app.services.connectors import store as cstore
from app.services.connectors.fieldmap import (
    extract_record,
    select_translatable_fields,
)
from app.services.connectors.salesforce import SalesforceConnector

# A throwaway RSA key so JWT signing works fully offline (the mock token endpoint
# does not verify the signature — it only needs a well-formed RS256 assertion).
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_KEY_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode()

_CC_VAL = "cc-" + "x9" * 6  # fixture value for the client-credentials mode

_DESCRIBE_FIELDS = [
    {"name": "Title", "type": "string", "label": "Title", "updateable": True, "calculated": False},
    {
        "name": "Summary",
        "type": "textarea",
        "label": "Summary",
        "updateable": True,
        "calculated": False,
    },
    {
        "name": "Body__c",
        "type": "textarea",
        "label": "Body",
        "updateable": True,
        "calculated": False,
        "htmlFormatted": True,
    },  # rich text -> html
    {
        "name": "FAQ_Answer__c",
        "type": "string",
        "label": "FAQ Answer",  # renamed/custom -> discovered
        "updateable": True,
        "calculated": False,
    },
    {
        "name": "Computed__c",
        "type": "string",
        "label": "Computed",  # formula -> excluded
        "updateable": True,
        "calculated": True,
    },
    {
        "name": "UrlName",
        "type": "string",
        "label": "URL Name",  # non-content -> excluded
        "updateable": True,
        "calculated": False,
    },
    {
        "name": "Views__c",
        "type": "double",
        "label": "Views",  # non-text -> excluded
        "updateable": True,
        "calculated": False,
    },
    {
        "name": "RecordTypeId",
        "type": "reference",
        "label": "Record Type",
        "updateable": True,
        "calculated": False,
    },
    {
        "name": "Language",
        "type": "picklist",
        "label": "Language",
        "updateable": False,
        "calculated": False,
    },
]
_RECORD_TYPES = [
    {"recordTypeId": "012000000001AAA", "name": "FAQ", "master": False, "available": True},
    {"recordTypeId": "012000000MASTER", "name": "Master", "master": True, "available": True},
]
_LANGUAGES = {
    "en_US": {"active": True},
    "de": {"active": True},
    "fr": {"active": True},
    "es": {"active": False},
}

_AID_RE = re.compile(r"KnowledgeArticleId = '([^']+)'")
_LANG_RE = re.compile(r"Language = '([^']+)'")
_IN_RE = re.compile(r"KnowledgeArticleId IN \(([^)]+)\)")
_RT_RE = re.compile(r"RecordTypeId = '([^']+)'")


def _aid(n: int) -> str:
    return f"kA{n:014d}"  # 16 chars, [a-zA-Z0-9] -> passes _SF_ID_RE


def _master(
    aid,
    title="Reset password",
    summary="How to reset",
    body="<h1>Reset</h1><p>Click the link.</p>",
    faq="Press the button.",
    rt="012000000001AAA",
):
    return {
        "Id": "kv" + aid[2:],
        "KnowledgeArticleId": aid,
        "ArticleNumber": "000001",
        "Title": title,
        "Summary": summary,
        "Body__c": body,
        "FAQ_Answer__c": faq,
        "Language": "en_US",
        "PublishStatus": "Online",
        "IsMasterLanguage": True,
        "RecordTypeId": rt,
        "LastModifiedDate": "2026-01-01T00:00:00.000+0000",
    }


class FakeSalesforce:
    def __init__(self, masters=None, fields=None, record_types=None, languages=None):
        self.masters = {m["KnowledgeArticleId"]: dict(m) for m in (masters or [])}
        self.fields = fields if fields is not None else list(_DESCRIBE_FIELDS)
        self.record_types = record_types if record_types is not None else list(_RECORD_TYPES)
        self.languages = languages if languages is not None else dict(_LANGUAGES)
        self.translations: dict = {}  # (aid, sf_lang) -> version dict
        self.patched: list = []  # (version_id, payload)
        self.completed: list = []  # version_id
        self.published: list = []  # (article_id, version_id)
        self.drafts: dict = {}  # (aid, sf_lang) -> Draft copy made from an Online translation
        self.deleted: list = []  # Draft version ids deleted by the connector
        self.drafts_created: list = []  # Online version ids a Draft copy was made from
        self.submits: list = []  # (article_id, sf_lang)
        self._vseq = 0
        self.fail_complete = False
        self.fail_sobject_patch = False
        self.fail_publish = False
        self.submit_returns_id = True
        self.instance = "https://myorg.my.salesforce.com"
        self.describe_gets: list = []  # Phase 3B: every Knowledge__kav describe fetch
        self.token_forms: list = []  # every form POSTed to the token endpoint
        self.page_size = 0  # >0: paginate SOQL results via nextRecordsUrl
        self.next_url_override = None  # hostile nextRecordsUrl
        self.statuses_seen: list = []  # (method, path, body) of every mutating call
        self.query_log: list = []

    def transport(self):
        return httpx.MockTransport(self._handle)

    def seed_translation(self, aid, sf_lang, status="Draft", **extra):
        self._vseq += 1
        vid = f"kav{self._vseq:013d}"
        rec = {
            "Id": vid,
            "KnowledgeArticleId": aid,
            "Language": sf_lang,
            "PublishStatus": status,
            "IsMasterLanguage": False,
            "VersionNumber": 1,
        }
        rec.update(extra)
        self.translations[(aid, sf_lang)] = rec
        return vid

    def _find(self, vid):
        for r in [*self.translations.values(), *self.drafts.values()]:
            if r["Id"] == vid:
                return r
        return None

    def _soql(self, q: str) -> list:
        if "IsMasterLanguage = true" in q:
            recs = list(self.masters.values())
            m = _IN_RE.search(q)
            if m:
                ids = {x.strip().strip("'") for x in m.group(1).split(",")}
                recs = [r for r in recs if r["KnowledgeArticleId"] in ids]
            rt = _RT_RE.search(q)
            if rt:
                recs = [r for r in recs if r.get("RecordTypeId") == rt.group(1)]
            return [dict(r) for r in recs]
        if "IsMasterLanguage = false" in q:
            am = _AID_RE.search(q)
            lm = _LANG_RE.search(q)
            if not am or not lm:
                return []
            sm = re.search(r"PublishStatus = '([A-Za-z]+)'", q)
            assert sm, "fake enforces the Knowledge__kav PublishStatus filter rule"
            key = (am.group(1), lm.group(1))
            cands = [r for r in (self.translations.get(key), self.drafts.get(key)) if r]
            return [dict(r) for r in cands if r["PublishStatus"] == sm.group(1)][:1]
        return []

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        if method != "GET" and not path.endswith("/services/oauth2/token"):
            self.statuses_seen.append((method, path, req.content.decode() if req.content else ""))
        if path.endswith("/services/oauth2/token"):
            from urllib.parse import parse_qs

            self.token_forms.append({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
            return httpx.Response(
                200,
                json={"access_token": "AT", "instance_url": self.instance, "token_type": "Bearer"},
            )
        if method == "GET" and path.endswith("/limits/"):
            return httpx.Response(
                200, json={"DailyApiRequests": {"Max": 100000, "Remaining": 99999}}
            )
        if method == "GET" and path.endswith("/knowledgeManagement/settings"):
            return httpx.Response(
                200, json={"defaultLanguage": "en_US", "languages": self.languages}
            )
        if method == "GET" and path.endswith("/sobjects/Knowledge__kav/describe"):
            self.describe_gets.append(path)  # Phase 3B: prove record_type_name caches
            return httpx.Response(
                200, json={"fields": self.fields, "recordTypeInfos": self.record_types}
            )
        if method == "GET" and "/query" in path:
            if path.rstrip("/").endswith("/query"):
                q = req.url.params.get("q", "")
                self.query_log.append(q)
                if "PublishStatus" not in q and "Id =" not in q:
                    return httpx.Response(
                        400,
                        json=[
                            {"errorCode": "MALFORMED_QUERY", "message": "must filter PublishStatus"}
                        ],
                    )
                recs = self._soql(q)
                if self.page_size and len(recs) > self.page_size:
                    self._pending = recs[self.page_size :]
                    return httpx.Response(
                        200,
                        json={
                            "done": False,
                            "records": recs[: self.page_size],
                            "nextRecordsUrl": self.next_url_override
                            or "/services/data/v65.0/query/01gXXPAGE2-2000",
                        },
                    )
                return httpx.Response(
                    200, json={"done": True, "totalSize": len(recs), "records": recs}
                )
            return httpx.Response(
                200, json={"done": True, "records": getattr(self, "_pending", [])}
            )
        if method == "POST" and path.endswith("/knowledgeManagement/articleVersions/translations"):
            body = json.loads(req.content)
            aid = body["articleId"]
            lang = body["assignments"][0]["language"]
            self.submits.append((aid, lang))
            existing = self.translations.get((aid, lang))
            if existing:
                return httpx.Response(
                    400, json=[{"errorCode": "DUPLICATE_VALUE", "message": "already translated"}]
                )
            else:
                vid = self.seed_translation(aid, lang, status="Draft")
            return httpx.Response(200, json=({"id": vid} if self.submit_returns_id else {}))
        if method == "POST" and path.endswith(
            "/actions/standard/createDraftFromOnlineKnowledgeArticle"
        ):
            inp = json.loads(req.content)["inputs"][0]
            assert inp["action"] == "EDIT_AS_DRAFT_TRANSLATION" and inp["unpublish"] is False, inp
            src = self._find(inp["articleVersionId"])
            if not src or src["PublishStatus"] != "Online":
                return httpx.Response(
                    200, json=[{"isSuccess": False, "errors": [{"statusCode": "NOT_ONLINE"}]}]
                )
            self._vseq += 1
            key = (src["KnowledgeArticleId"], src["Language"])
            draft = dict(src, Id=f"kav{self._vseq:013d}", PublishStatus="Draft")
            self.drafts[key] = draft
            self.drafts_created.append(src["Id"])
            return httpx.Response(200, json=[{"isSuccess": True, "errors": None}])
        if method == "POST" and path.endswith("/actions/standard/deleteKnowledgeArticles"):
            inp = json.loads(req.content)["inputs"][0]
            for vid in inp["articleVersionIdList"]:
                rec = self._find(vid)
                assert rec and rec["PublishStatus"] == "Draft", "only a Draft may be deleted"
                self.translations = {k: v for k, v in self.translations.items() if v["Id"] != vid}
                self.deleted.append(vid)
            return httpx.Response(200, json=[{"isSuccess": True, "errors": None}])
        if method == "POST" and path.endswith("/actions/standard/publishKnowledgeArticles"):
            inp = json.loads(req.content)["inputs"][0]
            assert inp["pubAction"] == "PUBLISH_TRANSLATION", inp
            if self.fail_publish:
                return httpx.Response(
                    200, json=[{"isSuccess": False, "errors": [{"statusCode": "PUBLISH_FAILED"}]}]
                )
            for vid in inp["articleVersionIdList"]:
                rec = self._find(vid)
                if not rec or rec["PublishStatus"] != "Draft":
                    return httpx.Response(
                        200, json=[{"isSuccess": False, "errors": [{"statusCode": "NOT_DRAFT"}]}]
                    )
                key = (rec["KnowledgeArticleId"], rec["Language"])
                rec["PublishStatus"] = "Online"
                self.translations[key] = self.drafts.pop(key, rec)  # replaces the old Online
                self.published.append((key[0], vid))
            return httpx.Response(200, json=[{"isSuccess": True, "errors": None}])
        if method == "PATCH" and "/knowledgeManagement/articleVersions/translations/" in path:
            vid = path.rsplit("/", 1)[-1]
            body = json.loads(req.content)
            if not self._find(vid):
                return httpx.Response(404, json=[{"errorCode": "NOT_FOUND"}])
            if "publishStatus" in body:  # undocumented for translations: must never be sent
                return httpx.Response(400, json=[{"errorCode": "UNDOCUMENTED_PUBLISH_PATCH"}])
            if self.fail_complete:
                return httpx.Response(400, json=[{"errorCode": "COMPLETE_FAILED"}])
            assert body == {"complete": "true"}, body
            self.completed.append(vid)
            return httpx.Response(204)
        if method == "POST" and "masterVersions" in path:
            return httpx.Response(
                404, json=[{"errorCode": "wrong-endpoint-for-translation-publish"}]
            )
        if method == "PATCH" and "/sobjects/Knowledge__kav/" in path:
            vid = path.rsplit("/", 1)[-1]
            payload = json.loads(req.content)
            self.patched.append((vid, payload))
            if self.fail_sobject_patch:
                return httpx.Response(400, json=[{"errorCode": "FIELD_INTEGRITY_EXCEPTION"}])
            rec = self._find(vid)
            if rec:
                assert rec["PublishStatus"] == "Draft", "only a Draft is editable"
                rec.update(payload)
            return httpx.Response(204)
        return httpx.Response(404, json={"error": path})


def _sconn(name, extra_cfg=None):
    cfg = {
        "login_url": "test.salesforce.com",
        "username": "svc@example.com",
        "source_locale": "en-US",
        "target_locales": "de, fr",
    }
    cfg.update(extra_cfg or {})
    sec = {"client_id": "3MVGxxx", "private_key": _PRIVATE_KEY_PEM}
    return cstore.create("salesforce", name, cfg, sec, actor="admin")


def _prep_fields(conn, fake):
    ok, msg, _s = cstore.salesforce_rediscover_fields(conn.id, transport=fake.transport())
    assert ok, msg


def main() -> None:
    init_db()
    from app.filters import get_filter

    # ---- (0) generalizer: select_translatable_fields buckets by type + exclusions ----
    picked = select_translatable_fields(_DESCRIBE_FIELDS)
    by = {f["name"]: f for f in picked}
    assert set(by) == {"Title", "Summary", "Body__c", "FAQ_Answer__c"}, by
    assert by["Title"]["type"] == "string" and by["Summary"]["type"] == "textarea"
    assert by["Body__c"]["type"] == "html", ("rich text -> html", by["Body__c"])
    assert by["FAQ_Answer__c"]["type"] == "string", ("renamed/custom field discovered", by)
    for gone in ("Computed__c", "UrlName", "Views__c", "RecordTypeId", "Language"):
        assert gone not in by, (f"{gone} must be excluded (formula/non-content/non-text/id)", by)
    print(
        "  generalizer: fields bucketed by type; formula+UrlName+non-text+id excluded; "
        "custom field discovered; rich-text -> html"
    )

    # ---- (0b) extract_record + build_payload round-trip (html + plain fields) ----
    ex = extract_record(_master(_aid(1)), picked, get_filter)
    assert ex.title_field == "Title" and ex.title_text == "Reset password"
    # body units: Summary(plain) + Body__c#0 + Body__c#1 (html) + FAQ_Answer__c(plain)
    assert len(ex.body_units) == 4, [(u.text, u.p_index) for u in ex.body_units]
    # simulate the flow's mapping (translate each unit by prefixing) + seg0 title
    mapping = {str(u.p_index): f"T:{u.text}" for u in ex.body_units}
    payload = ex.build_payload("T:Reset password", mapping)
    assert payload["Title"] == "T:Reset password"
    assert payload["Summary"] == "T:How to reset"
    assert payload["FAQ_Answer__c"] == "T:Press the button."
    assert payload["Body__c"] == "<h1>T:Reset</h1><p>T:Click the link.</p>", payload["Body__c"]
    print(
        "  generalizer: extract_record -> per-field units; build_payload reassembles "
        "title + plain + html fields"
    )

    # ---- (0c) unknown record type + missing fields: Title-only fallback, no crash ----
    ex2 = extract_record(
        {"Title": "Only title"}, [{"name": "Title", "type": "string", "label": "Title"}], get_filter
    )
    assert ex2.title_text == "Only title" and not ex2.body_units and not ex2.is_empty
    pay2 = ex2.build_payload("X", {})
    assert pay2 == {"Title": "X"}, pay2
    ex3 = extract_record({"Title": "", "Body__c": ""}, picked, get_filter)
    assert ex3.is_empty, ("all-empty record -> is_empty", ex3.title_text, ex3.body_units)
    # a plain field whose translation is EXCLUDED (NEEDS_REVIEW) is OMITTED, never source
    ex4 = extract_record(_master(_aid(1)), picked, get_filter)
    pay4 = ex4.build_payload(
        "T:Reset password",
        {u.p_index: f"T:{u.text}" for u in ex4.body_units if u.p_index != "FAQ_Answer__c"},
    )  # FAQ omitted
    assert "FAQ_Answer__c" not in pay4, ("an omitted plain field is NOT written as source", pay4)
    print(
        "  generalizer: unknown-type Title-only fallback; all-empty -> skip; omitted "
        "plain field never written as source"
    )

    # ---- (1) auth: JWT token mint + test() ----
    fake = FakeSalesforce(masters=[_master(_aid(1))])
    sc = SalesforceConnector(
        {"login_url": "test.salesforce.com", "username": "svc@x.com"},
        {"client_id": "cid", "private_key": _PRIVATE_KEY_PEM},
        transport=fake.transport(),
    )
    ok, msg = sc.test()
    assert ok, msg
    assert "active language" in msg, msg
    tok, instance = sc._auth()
    assert tok == "AT" and instance == "https://myorg.my.salesforce.com", (tok, instance)
    print("  auth: JWT-bearer mint (RS256) + instance_url trust; test() reports active languages")

    # ---- (2) locale mapping: BCP-47 <-> SF code + active rejection ----
    assert sc.sf_lang_for("de-DE") == "de", sc.sf_lang_for("de-DE")
    assert sc.sf_lang_for("en-US") == "en_US", sc.sf_lang_for("en-US")
    assert sc.sf_lang_for("fr") == "fr"
    assert sc.sf_lang_for("es-ES") is None, ("inactive language rejected", sc.sf_lang_for("es-ES"))
    assert sc.sf_lang_for("zh-CN") is None, ("unmapped language rejected", sc.sf_lang_for("zh-CN"))
    assert SalesforceConnector._sf_to_loc("zh_CN") == "zh-CN"
    print("  locale: de-DE->de, en-US->en_US, fr->fr; inactive(es)/unmapped(zh-CN) rejected")

    # ---- (3) containers (record types) + discover_articles ----
    assert [c["id"] for c in sc.containers()] == ["012000000001AAA"], sc.containers()
    arts = sc.discover_articles()
    assert [a["KnowledgeArticleId"] for a in arts] == [_aid(1)], arts
    scoped = sc.discover_articles(article_ids=[_aid(1)])
    assert [a["KnowledgeArticleId"] for a in scoped] == [_aid(1)], scoped
    assert sc.discover_articles(article_ids=["not-a-real-id"]) == [], "bad id -> empty"
    print(
        "  discovery: record-type containers (master excluded); discover_articles + "
        "KnowledgeArticleId scope + bad-id guard"
    )

    # ---- (4) rediscover-fields persists the per-record-type field map ----
    fk4 = FakeSalesforce(masters=[_master(_aid(1))])
    conn4 = _sconn("SF rediscover")
    ok, msg, s4 = cstore.salesforce_rediscover_fields(conn4.id, transport=fk4.transport())
    assert ok, msg
    fmap = (cstore.get(conn4.id).config or {}).get("sf_field_map") or {}
    assert "012000000001AAA" in fmap and "" in fmap, ("keyed per record type + default", list(fmap))
    names = {f["name"] for f in fmap[""]}
    assert names == {"Title", "Summary", "Body__c", "FAQ_Answer__c"}, names
    assert s4["fields"] == 4, s4
    print("  rediscover: sf_field_map persisted per record type (4 translatable fields)")

    # ---- (5) full flow CREATE: submit -> PATCH Draft -> set-complete, draft, no publish ----
    fk5 = FakeSalesforce(masters=[_master(_aid(1))])
    conn5 = _sconn("SF create")
    _prep_fields(conn5, fk5)
    ok, msg, s5 = cstore.salesforce_translate(conn5.id, actor="auto", transport=fk5.transport())
    assert ok, msg
    assert s5["created"] == 2 and s5["updated"] == 0, s5
    assert {l for (_a, l) in fk5.translations} == {"de", "fr"}, fk5.translations  # noqa: E741
    for lang in ("de", "fr"):
        rec = fk5.translations[(_aid(1), lang)]
        assert rec["PublishStatus"] == "Draft", ("draft by default", rec)
        assert rec["Title"].endswith("Reset password") and rec["Title"] != "Reset password"
        assert rec["Summary"].endswith("How to reset") and rec["Summary"] != "How to reset"
        assert "Reset" in rec["Body__c"] and "Click the link" in rec["Body__c"], rec["Body__c"]
        assert (
            rec["FAQ_Answer__c"].endswith("Press the button.")
            and rec["FAQ_Answer__c"] != "Press the button."
        )
        assert "PublishStatus" not in fk5.patched[0][1], (
            "PublishStatus is NEVER PATCHed (gotcha #3)"
        )
    assert len(fk5.completed) == 2 and not fk5.published, (
        "set-complete x2, no publish in draft mode",
        fk5.completed,
        fk5.published,
    )
    print(
        "  flow CREATE: 2 Draft versions (submit->PATCH->complete); title/plain/html "
        "translated; PublishStatus never PATCHed; no publish (draft)"
    )

    # ---- (5a2) Phase 3B context harvest: RecordType NAME -> content_kind ----
    # record_type_name resolves the opaque RecordTypeId to its Name and CACHES it:
    # one describe per pull, never per-article.
    fk5h = FakeSalesforce(masters=[_master(_aid(1))])
    sch = SalesforceConnector(
        {"login_url": "test.salesforce.com", "username": "svc@x.com"},
        {"client_id": "cid", "private_key": _PRIVATE_KEY_PEM},
        transport=fk5h.transport(),
    )
    assert sch.record_type_name("012000000001AAA") == "FAQ", "RecordTypeId -> Name"
    assert sch.record_type_name("012000000MASTER") == "Master", "master record type also resolves"
    assert sch.record_type_name("012000000MISSING") is None, "unknown id -> None (never a raw id)"
    assert sch.record_type_name("") is None and sch.record_type_name(None) is None
    assert len(fk5h.describe_gets) == 1, (
        "record_type_name caches: ONE describe for all lookups (per-pull, not per-article)",
        fk5h.describe_gets,
    )
    # end-to-end: the translate run stamps Document.context.content_kind = the RT name.
    fk5c = FakeSalesforce(masters=[_master(_aid(1)), _master(_aid(2))])
    conn5c = _sconn("SF harvest")
    _prep_fields(conn5c, fk5c)
    ok, msg, _s5c = cstore.salesforce_translate(conn5c.id, actor="auto", transport=fk5c.transport())
    assert ok, msg
    pid_on = cstore.get(conn5c.id).project_id
    with Session(engine) as s:
        docs = list(s.exec(select(Document).where(Document.project_id == pid_on)).all())
    ctxs = [d.context for d in docs if d.context]
    assert ctxs and all(c.get("content_kind") == "FAQ" for c in ctxs), (
        "every harvested SF doc carries content_kind=FAQ",
        ctxs,
    )
    assert all(c["source"]["system"] == "salesforce" for c in ctxs), ctxs
    # harvest OFF -> Document.context stays None (byte-stable).
    fk5d = FakeSalesforce(masters=[_master(_aid(3))])
    conn5d = _sconn("SF harvest off", {"harvest_context": "0"})
    _prep_fields(conn5d, fk5d)
    ok, _m, _s = cstore.salesforce_translate(conn5d.id, actor="auto", transport=fk5d.transport())
    assert ok
    pid_off = cstore.get(conn5d.id).project_id
    with Session(engine) as s:
        d_off = list(s.exec(select(Document).where(Document.project_id == pid_off)).all())
    assert d_off and all(d.context is None for d in d_off), (
        "harvest_context=0 -> Document.context None",
        [d.context for d in d_off],
    )
    print(
        "  Phase 3B harvest: RecordTypeId -> RecordType Name -> content_kind (one describe "
        "per pull, cached); harvest_context=0 -> no context"
    )

    # ---- (5b) publish toggle: the managed publish action fires (Draft -> Online) ----
    fk5b = FakeSalesforce(masters=[_master(_aid(1))])
    conn5b = _sconn("SF publish", {"publish": "1", "target_locales": "de"})
    _prep_fields(conn5b, fk5b)
    ok, msg, s5b = cstore.salesforce_translate(conn5b.id, actor="auto", transport=fk5b.transport())
    assert ok, msg
    assert s5b["created"] == 1 and fk5b.published, ("publish action fired", s5b, fk5b.published)
    assert fk5b.translations[(_aid(1), "de")]["PublishStatus"] == "Online", fk5b.translations
    print("  flow publish: publish=1 fires PUBLISH_TRANSLATION (Draft->Online)")

    # ---- (6) UPDATE: an existing Draft version is reused (no duplicate submit) ----
    fk6 = FakeSalesforce(masters=[_master(_aid(1))])
    vid_de = fk6.seed_translation(_aid(1), "de", status="Draft", Title="Alt", VersionNumber=1)
    conn6 = _sconn("SF update")
    _prep_fields(conn6, fk6)
    ok, msg, s6 = cstore.salesforce_translate(conn6.id, actor="auto", transport=fk6.transport())
    assert ok, msg
    assert s6["created"] == 1 and s6["updated"] == 1, ("fr created, de updated", s6)
    assert fk6.translations[(_aid(1), "de")]["Id"] == vid_de, "reused the existing Draft version"
    assert fk6.submits.count((_aid(1), "de")) == 0, (
        "no re-submit for an existing Draft",
        fk6.submits,
    )
    assert fk6.translations[(_aid(1), "de")]["Title"] != "Alt", "Draft fields updated"
    print("  flow UPDATE: existing Draft reused (no duplicate submit), fields updated; fr created")

    # ---- (7) content-hash suppression: unchanged source -> skipped, no writes ----
    fk7 = FakeSalesforce(masters=[_master(_aid(1))])
    conn7 = _sconn("SF hash")
    _prep_fields(conn7, fk7)
    ok, _m, s7a = cstore.salesforce_translate(conn7.id, actor="auto", transport=fk7.transport())
    assert s7a["created"] == 2, s7a
    fk7.patched.clear()
    ok, _m, s7b = cstore.salesforce_translate(conn7.id, actor="auto", transport=fk7.transport())
    assert ok and s7b["created"] == 0 and s7b["updated"] == 0 and s7b["skipped"] == 2, (
        "unchanged content -> suppressed",
        s7b,
    )
    assert not fk7.patched, ("no PATCH on unchanged content", fk7.patched)
    # real edit -> re-translated
    fk7.masters[_aid(1)]["Body__c"] = "<h1>Reset</h1><p>New steps.</p>"
    ok, _m, s7c = cstore.salesforce_translate(conn7.id, actor="auto", transport=fk7.transport())
    assert ok and s7c["updated"] == 2 and s7c["skipped"] == 0, ("real edit -> re-translate", s7c)
    print("  flow suppression: unchanged content -> 2 skipped, 0 writes; real edit -> 2 updated")

    # ---- (8) empty article -> skipped, no write ----
    fk8 = FakeSalesforce(masters=[_master(_aid(1), title="", summary="", body="", faq="")])
    conn8 = _sconn("SF empty")
    _prep_fields(conn8, fk8)
    ok, msg, s8 = cstore.salesforce_translate(conn8.id, actor="auto", transport=fk8.transport())
    assert ok, msg
    assert s8["empty_skipped"] == 1 and s8["created"] == 0 and not fk8.translations, s8
    print("  flow empty: an all-empty article is skipped (no write)")

    # ---- (9) mid-lifecycle failure at step 3 (set-complete) leaves Draft, no publish, FAILS ----
    fk9 = FakeSalesforce(masters=[_master(_aid(1))])
    fk9.fail_complete = True
    conn9 = _sconn("SF fail3", {"publish": "1", "target_locales": "de"})
    _prep_fields(conn9, fk9)
    ok, msg, s9 = cstore.salesforce_translate(conn9.id, actor="auto", transport=fk9.transport())
    assert not ok, ("a set-complete failure must FAIL the run", msg, s9)
    assert s9["created"] == 0 and s9["failures"], s9
    # the Draft was submitted + PATCHed but NEVER published (no partial publish)
    assert fk9.translations[(_aid(1), "de")]["PublishStatus"] == "Draft", fk9.translations
    assert not fk9.published, ("never publishes a partial", fk9.published)
    with Session(engine) as sdb:
        assert sdb.get(Connection, conn9.id).status == "error", "failure -> status error"
    print(
        "  flow failure: set-complete fails -> per-pair failure, Draft left, no publish, run FAILS"
    )

    # ---- (10) WriteBudget ceiling trims the run (partial, reported) ----
    from app import config as app_config

    real_ceiling = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        fk10 = FakeSalesforce(masters=[_master(_aid(1))])
        conn10 = _sconn("SF budget")
        _prep_fields(conn10, fk10)
        ok, msg, s10 = cstore.salesforce_translate(
            conn10.id, actor="auto", transport=fk10.transport()
        )
        assert ok, msg
        assert s10["created"] == 1 and s10["ceiling_hit"] is True and s10["write_ceiling"] == 1, s10
        assert len(fk10.translations) == 1, ("only one write under the ceiling", fk10.translations)
        assert "write ceiling" in msg, msg
    finally:
        app_config.MAX_WRITES_PER_RUN = real_ceiling
    print("  flow ceiling: WriteBudget=1 stops after 1 version (ceiling_hit reported)")

    # ---- (11) NEEDS_REVIEW body unit excluded from write-back (source kept in that field) ----
    import app.services.translate as translate_mod

    real_ptd = translate_mod.pretranslate_document

    def _ptd_flip_de_body(session, doc_id, locale, spec=None, force_pii=False):
        stats = real_ptd(session, doc_id, locale, spec, force_pii=force_pii)
        if locale == "de":
            seg = session.exec(
                select(Segment).where(
                    Segment.document_id == doc_id,
                    Segment.target_locale == "de",
                    Segment.source_text == "Click the link.",
                )
            ).first()
            if seg:
                seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
                session.commit()
        return stats

    set_pieces("app.services.translate", "pretranslate_document", _ptd_flip_de_body)
    try:
        fk11 = FakeSalesforce(masters=[_master(_aid(1))])
        conn11 = _sconn("SF needs-review")
        _prep_fields(conn11, fk11)
        ok, msg, _s11 = cstore.salesforce_translate(
            conn11.id, actor="auto", transport=fk11.transport()
        )
        assert ok, msg
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    # #640: a pair with a held unit is NOT created (the held spot would carry SOURCE text: a
    # mixed-language target); nothing reaches the wire and the next run retries it.
    assert (_aid(1), "de") not in fk11.translations, ("held pair: no de translation created",)
    fr_body = fk11.translations[(_aid(1), "fr")]["Body__c"]
    assert "<p>Click the link.</p>" not in fr_body, ("fr's unit WAS translated", fr_body)
    print("  defense-in-depth: a NEEDS_REVIEW body unit holds the pair (no de translation created)")

    # ---- (12) all-rejected (no active/mapped target) -> run FAILS + status error ----
    fk12 = FakeSalesforce(masters=[_master(_aid(1))])
    conn12 = _sconn("SF all-rejected", {"target_locales": "es-ES, zh-CN"})  # inactive + unmapped
    _prep_fields(conn12, fk12)
    ok, msg, s12 = cstore.salesforce_translate(conn12.id, actor="auto", transport=fk12.transport())
    assert not ok, ("all-rejected must FAIL", msg, s12)
    assert (
        s12["locales"] == []
        and set(s12["rejected"]) == {"es-es", "zh-cn"}
        and not fk12.translations
    ), s12
    print("  flow all-rejected: inactive(es)+unmapped(zh-CN) -> ok=False, nothing written")

    # ---- (13) engine-consumer regression: NO watermark key is ever written ----
    cfg13 = cstore.get(conn7.id).config or {}
    assert "sf_hashes" in cfg13, ("the content-hash map IS persisted", list(cfg13))
    assert not any("watermark" in k for k in cfg13), (
        "the poll-first flow must NOT introduce a watermark key",
        list(cfg13),
    )
    print("  regression: flow persists sf_hashes but NO watermark key (poll-first)")

    # ---- (14) submit-for-translation with NO id in response -> SOQL fallback finds the Draft ----
    fk14 = FakeSalesforce(masters=[_master(_aid(1))])
    fk14.submit_returns_id = False  # bare 200, no id -> connector must SOQL for the Draft version
    conn14 = _sconn("SF submit-fallback", {"target_locales": "de"})
    _prep_fields(conn14, fk14)
    ok, msg, s14 = cstore.salesforce_translate(conn14.id, actor="auto", transport=fk14.transport())
    assert ok and s14["created"] == 1, ("SOQL fallback located the created Draft", s14, msg)
    assert fk14.translations[(_aid(1), "de")]["Title"], fk14.translations
    print("  robustness: submit-for-translation w/o id in body -> SOQL fallback locates the Draft")

    # ---- (15) org isolation: a foreign/missing cid -> 404 on the /api translate route ----
    c = TestClient(app)
    from tests._api_fixture import user_api_token

    connapi = _sconn("SF api")
    hdr = {"X-Loc-Token": user_api_token()}
    r_ghost = c.post("/api/connectors/99999999/salesforce/translate", json={}, headers=hdr)
    assert r_ghost.status_code == 404, ("foreign/missing cid -> 404", r_ghost.status_code)
    r_own = c.post(
        f"/api/connectors/{connapi.id}/salesforce/translate", json={"article_ids": []}, headers=hdr
    )
    assert r_own.status_code in (200, 400), (
        "own connection reachable",
        r_own.status_code,
        r_own.text,
    )
    r_red = c.post("/api/connectors/99999999/salesforce/rediscover-fields", json={}, headers=hdr)
    assert r_red.status_code == 404, ("rediscover foreign cid -> 404", r_red.status_code)
    print("  org isolation: foreign/missing cid -> 404 on both /api salesforce routes")

    # ---- (16) routes: Salesforce in the add-connector flow + rediscover panel + inbound card ----
    idx = c.get("/connectors").text
    assert "Salesforce" in idx, "Salesforce tile missing from /connectors"
    typ = c.get("/connectors/salesforce").text
    # Rebuilt + un-archived (rebuild): the add form is offered again.
    assert "New connections are closed" not in typ and "Create connection" in typ, (
        "un-archived Salesforce type page must offer the add form"
    )
    assert not _reg.is_archived("salesforce")
    _fnames = {f.name for f in _reg.spec("salesforce").config_fields} | {
        f.name for f in _reg.spec("salesforce").secret_fields
    }
    assert "username" in _fnames, ("Salesforce spec must still declare 'username'", _fnames)
    page = c.get(f"/connectors/connection/{connapi.id}").text
    assert "/salesforce/translate" in page, "inbound-API endpoint path must render (P1)"
    assert "/salesforce/rediscover-fields" in page, "rediscover-fields panel must render"
    print("  routes: Salesforce tile + type page; inbound-API endpoint + rediscover panel present")

    # ---- (17) CLI salesforce-translate + salesforce-rediscover-fields smoke ----
    from app.cli.cmd_connector import (
        _salesforce_rediscover_fields,
        _salesforce_translate,
    )

    fkcli = FakeSalesforce(masters=[_master(_aid(1))])
    conncli = _sconn("SF cli")

    class _Args:
        connection_id = conncli.id
        article_ids = _aid(1)
        locales = ""
        actor = "cli"
        local = True
        org = None
        json = False

    class _Emit:
        def __init__(self):
            self.res = None

        def result(self, msg, data):
            self.res = (msg, data)

    real_st = cstore.salesforce_translate
    real_rd = cstore.salesforce_rediscover_fields
    seen = {}

    def _cap_st(
        cid,
        article_ids=None,
        target_locales=None,
        actor="",
        transport=None,
        only_pairs=None,
        source="manual",
    ):
        seen["article_ids"] = article_ids
        return real_st(
            cid,
            article_ids=article_ids,
            target_locales=target_locales,
            actor=actor,
            transport=fkcli.transport(),
            only_pairs=only_pairs,
        )

    def _cap_rd(cid, actor="", transport=None):
        return real_rd(cid, actor=actor, transport=fkcli.transport())

    set_pieces("app.services.connectors.store", "salesforce_translate", _cap_st)
    set_pieces("app.services.connectors.store", "salesforce_rediscover_fields", _cap_rd)
    try:
        emit = _Emit()
        assert _salesforce_rediscover_fields(_Args(), emit) == 0, emit.res
        emit2 = _Emit()
        rc = _salesforce_translate(_Args(), emit2)
        assert rc == 0, emit2.res
        assert seen.get("article_ids") == [_aid(1)], seen
        assert emit2.res[1]["ok"], emit2.res
    finally:
        set_pieces("app.services.connectors.store", "salesforce_translate", real_st)
        set_pieces("app.services.connectors.store", "salesforce_rediscover_fields", real_rd)
    print("  CLI: salesforce-rediscover-fields + salesforce-translate parse args and run (OK)")

    # ---- (18) retry_failed re-runs ONLY the failed (article, locale) pairs ----
    fkr = FakeSalesforce(masters=[_master(_aid(1)), _master(_aid(2), title="Other")])
    connr = _sconn("SF retry")
    _prep_fields(connr, fkr)
    failures = [
        {"article_id": _aid(1), "locale": "de", "error": "500"},
        {"article_id": _aid(2), "locale": "fr", "error": "400"},
    ]
    okr, _m, _sr = cstore.retry_failed(connr.id, failures, actor="auto", transport=fkr.transport())
    assert okr, _sr
    written = set(fkr.translations)
    assert written == {(_aid(1), "de"), (_aid(2), "fr")}, (
        "only the 2 failed pairs written",
        written,
    )
    print("  retry: re-ran ONLY the 2 failed (article, locale) pairs, not the cross product")

    # ---- (19) FINDING 1: locale fold dedup - fr-FR + fr collide on SF code 'fr' ----
    # Both fr-FR and fr resolve (exact-then-base-strip) onto the SAME Salesforce
    # language code 'fr'. Without dedup, both are 'usable' and write the SAME
    # translation version sequentially (last-writer-wins clobber + a redundant paid
    # MT run), while BOTH record a success hash.
    fk19 = FakeSalesforce(masters=[_master(_aid(1))])
    conn19 = _sconn("SF locale-fold", {"target_locales": "fr-FR, fr"})
    _prep_fields(conn19, fk19)
    ok, msg, s19 = cstore.salesforce_translate(conn19.id, actor="auto", transport=fk19.transport())
    assert ok, msg
    assert s19["created"] == 1 and s19["updated"] == 0, (
        "exactly ONE lifecycle write to fr, not two",
        s19,
    )
    assert {l for (_a, l) in fk19.translations} == {"fr"}, fk19.translations  # noqa: E741
    assert s19["locales"] == ["fr"], ("only the canonical locale is usable", s19["locales"])
    assert any("collides with" in r and "fr" in r for r in s19["rejected"]), (
        "the colliding locale must be reported rejected with a clear reason",
        s19["rejected"],
    )
    cfg19 = cstore.get(conn19.id).config or {}
    hash_keys19 = [k for k in (cfg19.get("sf_hashes") or {}) if k.startswith(_aid(1) + ":")]
    assert len(hash_keys19) == 1, ("only ONE hash entry for the collision, not two", hash_keys19)
    print(
        "  FINDING 1: fr-FR + fr collide on Salesforce code 'fr' -> ONE write, "
        "collider rejected, ONE hash entry"
    )

    # ---- (20) FINDING 2: draft-then-publish-flip must not get stuck ----
    # Run 1 (publish=0) creates a Draft + records the content hash. Flip publish=1
    # and re-run with UNCHANGED source: the pair must NOT be suppressed just because
    # the hash matches and a translation exists -- an existing Draft (not yet
    # Online) must complete the lifecycle to Online, not sit stuck forever.
    fk20 = FakeSalesforce(masters=[_master(_aid(1))])
    conn20 = _sconn("SF draft-then-publish", {"target_locales": "de", "publish": "0"})
    _prep_fields(conn20, fk20)
    ok, msg, s20a = cstore.salesforce_translate(conn20.id, actor="auto", transport=fk20.transport())
    assert ok and s20a["created"] == 1, s20a
    assert fk20.translations[(_aid(1), "de")]["PublishStatus"] == "Draft", fk20.translations
    assert not fk20.published, ("draft mode must not publish", fk20.published)
    # flip to publish=1, unchanged source, re-run
    cstore.patch_config(conn20.id, {"publish": "1"})
    ok, msg, s20b = cstore.salesforce_translate(conn20.id, actor="auto", transport=fk20.transport())
    assert ok, msg
    assert s20b["skipped"] == 0, ("the Draft pair must NOT be suppressed on the publish-flip", s20b)
    assert fk20.published, ("the publish managed-action must fire", fk20.published)
    assert fk20.translations[(_aid(1), "de")]["PublishStatus"] == "Online", fk20.translations
    print("  FINDING 2: draft-then-publish-flip with unchanged source reaches Online (not stuck)")

    # ---- (21) taxonomy C: the probe filters PublishStatus and sees Draft AND Online ----
    fk21 = FakeSalesforce(masters=[_master(_aid(1))])
    fk21.seed_translation(_aid(1), "de", status="Online", Title="Live")
    sc21 = SalesforceConnector(
        {"login_url": "test.salesforce.com", "username": "svc@x.com"},
        {"client_id": "cid", "private_key": _PRIVATE_KEY_PEM},
        transport=fk21.transport(),
    )
    got = sc21.get_translation(_aid(1), "de-DE")
    assert got["PublishStatus"] == "Online", got
    assert all("PublishStatus = " in q for q in fk21.query_log), fk21.query_log
    assert [q for q in fk21.query_log if "'Draft'" in q] and [
        q for q in fk21.query_log if "'Online'" in q
    ], ("Draft probed first, then Online", fk21.query_log)
    print("  probe: Draft then Online, every query carries PublishStatus (SOQL rule)")

    # ---- (22) an ONLINE (live) translation is never overwritten unless refresh_online=1 ----
    fk22 = FakeSalesforce(masters=[_master(_aid(1))])
    vid22 = fk22.seed_translation(_aid(1), "de", status="Online", Title="Live")
    conn22 = _sconn("SF live", {"target_locales": "de"})
    _prep_fields(conn22, fk22)
    ok, msg, s22 = cstore.salesforce_translate(conn22.id, actor="auto", transport=fk22.transport())
    assert not ok and s22["failures"], ("live translation -> per-pair failure", msg, s22)
    assert "Online" in s22["failures"][0]["error"], s22["failures"]
    assert not fk22.patched and not fk22.drafts_created and not fk22.submits, (
        "live translation untouched",
        fk22.patched,
        fk22.drafts_created,
    )
    assert fk22.translations[(_aid(1), "de")]["Title"] == "Live"
    cstore.patch_config(conn22.id, {"refresh_online": "1"})
    ok, msg, s22b = cstore.salesforce_translate(conn22.id, actor="auto", transport=fk22.transport())
    assert ok and s22b["updated"] == 1, (msg, s22b)
    assert fk22.drafts_created == [vid22] and fk22.patched, (fk22.drafts_created, fk22.patched)
    # the LIVE version stays Online and untouched; the edit lands on a separate Draft copy
    assert fk22.translations[(_aid(1), "de")]["PublishStatus"] == "Online"
    assert fk22.translations[(_aid(1), "de")]["Title"] == "Live", "live content kept"
    assert fk22.drafts[(_aid(1), "de")]["Title"] != "Live"
    assert fk22.drafts[(_aid(1), "de")]["PublishStatus"] == "Draft", "publish=0 stays Draft"
    print("  live: Online translation untouched by default; refresh_online=1 edits it via Draft")

    # ---- (23) client-credentials auth: no username / private key, secret never surfaces ----
    fk23 = FakeSalesforce(masters=[_master(_aid(1))])
    cfg23 = {
        "login_url": "myorg.my.salesforce.com",
        "source_locale": "en-US",
        "target_locales": "de",
    }
    sec23 = {"client_id": "3MVGcc", "client_secret": _CC_VAL}
    conn23 = cstore.create("salesforce", "SF cc", cfg23, sec23, actor="admin")
    _prep_fields(conn23, fk23)
    ok, msg, s23 = cstore.salesforce_translate(conn23.id, actor="auto", transport=fk23.transport())
    assert ok and s23["created"] == 1, (msg, s23)
    form = fk23.token_forms[0]
    assert form["grant_type"] == "client_credentials" and "assertion" not in form, form
    assert _CC_VAL not in msg and _CC_VAL not in json.dumps(s23)
    bad = SalesforceConnector({"login_url": "myorg.my.salesforce.com"}, {"client_id": "x"})
    try:
        bad._auth()
        raise AssertionError("missing credentials must raise")
    except Exception as e:
        assert "EITHER" in str(e), e
    print("  auth: client-credentials mint (no cert, no username); secret not echoed")

    # ---- (24) pagination follows nextRecordsUrl; a hostile cursor fails closed (taxonomy A) ----
    fk24 = FakeSalesforce(masters=[_master(_aid(1)), _master(_aid(2))])
    fk24.page_size = 1
    sc24 = SalesforceConnector(
        {"login_url": "test.salesforce.com", "username": "svc@x.com"},
        {"client_id": "cid", "private_key": _PRIVATE_KEY_PEM},
        transport=fk24.transport(),
    )
    assert {a["KnowledgeArticleId"] for a in sc24.discover_articles()} == {_aid(1), _aid(2)}
    fk24.next_url_override = "https://evil.example/services/data/v65.0/query/01g"
    try:
        sc24.discover_articles()
        raise AssertionError("hostile nextRecordsUrl must be refused")
    except Exception as e:
        assert "nextRecordsUrl" in str(e), e
    fk24.next_url_override = "/services/data/v65.0/query/../../sobjects/User"
    try:
        sc24.discover_articles()
        raise AssertionError("traversal cursor must be refused")
    except Exception as e:
        assert "nextRecordsUrl" in str(e), e
    print("  discovery: nextRecordsUrl paginated; absolute and traversal cursors refused")

    # ---- (25) taxonomy A: hostile article ids / version ids never reach a URL or SOQL ----
    for evil in ("x' OR Id != '", "../../x", "kA00000000000001%0d%0a", "a" * 40):
        assert sc24.discover_articles(article_ids=[evil]) == [], evil
    ok_, why = sc24._patch_version("../../sobjects/User/005", {"Title": "x"})
    assert not ok_ and "invalid" in why
    ok_, why = sc24._publish("kA00000000000001", "a/b")
    assert not ok_ and "invalid" in why
    fk25 = FakeSalesforce(masters=[_master(_aid(1))])
    orig_handle = fk25._handle

    def _evil_submit(req, _h=orig_handle):
        if req.method == "POST" and req.url.path.endswith("/articleVersions/translations"):
            return httpx.Response(200, json={"id": "../../sobjects/User/005"})
        return _h(req)

    fk25._handle = _evil_submit
    conn25 = _sconn("SF evil vid", {"target_locales": "de"})
    _prep_fields(conn25, fk25)
    ok, msg, _s25 = cstore.salesforce_translate(conn25.id, actor="auto", transport=fk25.transport())
    assert not ok and not fk25.patched, ("a path-like version id is refused", msg, fk25.patched)
    print("  scope: injected ids and path-like version ids refused before any request")

    # ---- (26) vendor error codes surface as codes, never the free-text body ----
    fk26 = FakeSalesforce(masters=[_master(_aid(1))])
    fk26.fail_publish = True
    conn26 = _sconn("SF pubfail", {"publish": "1", "target_locales": "de"})
    _prep_fields(conn26, fk26)
    ok, msg, s26 = cstore.salesforce_translate(conn26.id, actor="auto", transport=fk26.transport())
    assert not ok and "PUBLISH_FAILED" in json.dumps(s26["failures"]), s26["failures"]
    assert fk26.translations[(_aid(1), "de")]["PublishStatus"] == "Draft", "Draft left intact"
    assert not any("publishStatus" in b for m, p, b in fk26.statuses_seen if "translations/" in p)
    print("  errors: publish failure -> errorCode in the failure row, Draft intact")

    # ---- (26b) a failed field PATCH on a FRESH Draft must not leave a source-language copy ----
    fk26b = FakeSalesforce(masters=[_master(_aid(1))])
    fk26b.fail_sobject_patch = True
    conn26b = _sconn("SF patchfail", {"publish": "1", "target_locales": "de"})
    _prep_fields(conn26b, fk26b)
    ok, msg, s26b = cstore.salesforce_translate(
        conn26b.id, actor="auto", transport=fk26b.transport()
    )
    assert not ok and s26b["failures"], s26b
    assert not fk26b.published and not fk26b.completed, "never completes or publishes"
    assert len(fk26b.deleted) == 1 and (_aid(1), "de") not in fk26b.translations, (
        "the fresh source-language Draft is discarded so the next run recreates it",
        fk26b.deleted,
    )
    print("  held-back safety: failed PATCH on a fresh Draft discards the source-language copy")

    # ---- (26c) Review gate: held units never ship source text, never complete or publish ----
    def _ptd_hold(texts):
        def _ptd(session, doc_id, locale, spec=None, force_pii=False):
            stats = real_ptd(session, doc_id, locale, spec, force_pii=force_pii)
            if locale == "de":
                for seg in session.exec(
                    select(Segment).where(
                        Segment.document_id == doc_id, Segment.target_locale == "de"
                    )
                ).all():
                    if texts is None or seg.source_text in texts:
                        seg.status = SegStatus.NEEDS_REVIEW
                        session.add(seg)
                session.commit()
            return stats

        return _ptd

    # ALL units held, an existing de Draft already holds a translation: nothing is sent.
    fk26c = FakeSalesforce(masters=[_master(_aid(1))])
    dvid = fk26c.seed_translation(_aid(1), "de", Title="Passwort", Body__c="<p>alt</p>")
    conn26c = _sconn("SF all held", {"publish": "1", "target_locales": "de"})
    _prep_fields(conn26c, fk26c)
    set_pieces("app.services.translate", "pretranslate_document", _ptd_hold(None))
    try:
        ok, msg, s26c = cstore.salesforce_translate(
            conn26c.id, actor="auto", transport=fk26c.transport()
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert not ok and "held-back" in json.dumps(s26c["failures"]), s26c
    assert not fk26c.patched and not fk26c.completed and not fk26c.published, (
        "an all-held pair writes nothing",
        fk26c.patched,
    )
    assert fk26c.translations[(_aid(1), "de")]["Title"] == "Passwort"
    # ALL units held and NO de version yet: no submit-for-translation (a source copy).
    fk26d = FakeSalesforce(masters=[_master(_aid(1))])
    conn26d = _sconn("SF all held new", {"publish": "1", "target_locales": "de"})
    _prep_fields(conn26d, fk26d)
    set_pieces("app.services.translate", "pretranslate_document", _ptd_hold(None))
    try:
        ok, msg, s26d = cstore.salesforce_translate(
            conn26d.id, actor="auto", transport=fk26d.transport()
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert not ok and not fk26d.submits and not fk26d.translations, (fk26d.submits, s26d)
    # ONE body sub-unit held on an existing Draft (publish=1): the usable fields are written,
    # the html field with the held unit keeps its stored value (no source text), and the
    # Draft is neither completed nor published.
    fk26e = FakeSalesforce(masters=[_master(_aid(1))])
    evid = fk26e.seed_translation(_aid(1), "de", Title="Passwort", Body__c="<p>alt</p>")
    conn26e = _sconn("SF partial held", {"publish": "1", "target_locales": "de"})
    _prep_fields(conn26e, fk26e)
    set_pieces("app.services.translate", "pretranslate_document", _ptd_hold({"Click the link."}))
    try:
        ok, msg, _s26e = cstore.salesforce_translate(
            conn26e.id, actor="auto", transport=fk26e.transport()
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    sent = [p for v, p in fk26e.patched if v == evid]
    assert sent and all("Body__c" not in p for p in sent), ("held html field not sent", sent)
    assert all(p.get("Title") != "Reset password" for p in sent), sent
    assert not any("_held" in k for p in sent for k in p), ("marker never on the wire", sent)
    assert fk26e.translations[(_aid(1), "de")]["Body__c"] == "<p>alt</p>"
    assert not fk26e.completed and not fk26e.published, (fk26e.completed, fk26e.published)
    assert fk26e.translations[(_aid(1), "de")]["PublishStatus"] == "Draft"
    assert dvid and evid and "held for review" in msg, msg
    print("  held-back safety: held units never ship source text and never complete or publish")

    # ---- (27) 429 on the SOQL transport is retried via base._request (Retry-After) ----
    from app.services.connectors import base as cbase

    fk27 = FakeSalesforce(masters=[_master(_aid(1))])
    inner27 = fk27._handle
    hits27 = {"n": 0}

    def _rate(req, _h=inner27):
        if req.method == "GET" and req.url.path.rstrip("/").endswith("/query") and not hits27["n"]:
            hits27["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return _h(req)

    slept27: list = []
    real_sleep27 = cbase._sleep
    cbase._sleep = lambda sec: slept27.append(sec)
    try:
        sc27 = SalesforceConnector(
            {"login_url": "test.salesforce.com", "username": "svc@x.com"},
            {"client_id": "cid", "private_key": _PRIVATE_KEY_PEM},
            transport=httpx.MockTransport(_rate),
        )
        assert [a["KnowledgeArticleId"] for a in sc27.discover_articles()] == [_aid(1)]
    finally:
        cbase._sleep = real_sleep27
    assert slept27 and slept27[0] == 3, ("Retry-After honored", slept27)
    print("  rate limit: 429 + Retry-After retried through base._request")

    # ---- (28) the spec is un-archived and the token never reaches the connector log ----
    assert not _reg.spec("salesforce").archived
    import logging

    class _Cap(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    cap = _Cap()
    lg = logging.getLogger("loc_tms.connectors")
    lg.addHandler(cap)
    try:
        cstore.salesforce_translate(conn5.id, actor="auto", transport=fk5.transport())
    finally:
        lg.removeHandler(cap)
    assert not any(("Bearer AT" in x) or (_CC_VAL in x) or ("BEGIN" in x) for x in cap.lines), (
        cap.lines
    )
    print("  posture: spec un-archived; no token or key in connector logs")

    print("verify_salesforce OK")


if __name__ == "__main__":
    main()
