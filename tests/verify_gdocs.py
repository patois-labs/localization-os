"""Google Docs connector (Drive folder of Docs, service account) - fully offline.

FakeGoogle is an httpx.MockTransport that models the parts of Google Drive v3 and Google Docs v1
this connector depends on (taxonomy G): the service-account JWT bearer token endpoint (the RS256
signature, iss/scope/aud/exp and the optional `sub` are all verified), folder listing with
`q` filters and nextPageToken paging, per-file visibility (a file is visible only when it or an
ancestor is shared to the service account; everything else answers 404), files.copy / create /
update (rename, trash), a service-account storage quota (copies in a plain My Drive folder fail
with storageQuotaExceeded), and a REAL structural Docs model: tabs, headers, footers, footnotes,
tables, lists, inline objects, text runs with styles and links, UTF-16 indices, atomic batchUpdate
(insertText, deleteContentRange, updateTextStyle) with `requiredRevisionId` (400 on a stale
revision), insert-inherits-style, and header / tab ids that a copy renumbers. The fake fails the
suite if a write lands outside the target folder.

Covers the taxonomy A-H checklist (the connector build blueprint section 3):
  A  folder ids / URLs / explicit document ids / impersonation user are validated fail-closed;
     a foreign, unshared, target-side, folder, non-Doc or trashed id is refused with zero writes
     and zero document reads; only googleapis.com is ever contacted.
  B  a held-back unit never creates or changes a Doc (all-or-nothing per document); a Doc edited
     by hand is never overwritten (zero MT spend, revision race, trash-to-regenerate).
  C  steady state: run 2 reads nothing and writes nothing; a rename of the source is not a change;
     a content edit re-translates just that Doc and the id map follows the new copy.
  D  only Google Docs are translated; Sheets, PDFs, shortcuts, trashed files and bad names are
     skipped WITH a reason; a document with pending suggestions is skipped, not guessed at.
  E  the Max documents cap round-robins across sub-folders (distribution asserted).
  F  a held or failed pair records no content hash; a healthy re-run completes it.
  G  the fake enforces Google's own rules (indices, atomic batch, revision, visibility, quota).
  H  WriteBudget ceiling, egress guard, key / token never in a message or log, parity surfaces.
Fidelity: the translated Doc keeps every paragraph style, list, table, inline object, header,
footer, footnote and tab of the source, and a link / bold span keeps its style after translation.

ASCII-only prints; final line is 'verify_gdocs OK'.
"""

# ruff: noqa: E501
import copy
import json
import logging
import os
import re
import sys
from urllib.parse import parse_qs, unquote

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_gdocs_")

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import config as app_config
from app.db import engine, init_db
from app.main import app
from app.models import Connection
from app.services.connectors import base as cbase
from app.services.connectors import idmap
from app.services.connectors import store as cstore
from app.services.connectors.gdocs import GoogleDocsConnector
from app.services.connectors.gdocs_google import (
    DOC_MIME,
    FOLDER_MIME,
    PENDING_PREFIX,
    SHORTCUT_MIME,
    TOKEN_URL,
    parse_folder_ref,
    parse_units,
    u16,
    validate_file_id,
)
from app.services.connectors.store_part2 import _has_credential

SA_EMAIL = "loc-os@fixture-proj.iam.gserviceaccount.com"
KEY_ID = "0123456789abcdef0123456789abcdef01234567"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUB = _KEY.public_key()
PEM_BODY = "".join(ln for ln in PEM.splitlines() if not ln.startswith("-----"))
SA_JSON = json.dumps(
    {
        "type": "service_account",
        "project_id": "fixture-proj",
        "private_key_id": KEY_ID,  # pregate: allow-secret offline fixture
        "private_key": PEM,  # pregate: allow-secret offline fixture
        "client_email": SA_EMAIL,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
)

ROOT_ID = "0RootFolderNotShared00"
SRC = "1SrcFolderAAAAAAAAAAAA"
TGT = "1TgtFolderBBBBBBBBBBBB"
OTHER = "1OtherFolderCCCCCCCCCC"
DRIVE_H, DOCS_H, TOKEN_H = "www.googleapis.com", "docs.googleapis.com", "oauth2.googleapis.com"
STYLE_FIELDS = {
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "smallCaps",
    "backgroundColor",
    "foregroundColor",
    "fontSize",
    "weightedFontFamily",
    "baselineOffset",
    "link",
}


# ============================================================================
# The Docs model.
# ============================================================================
def T(text, **style):
    return ("t", text, style)


def OBJ(kind):
    return ("o", kind, {})


def P(*runs, named="NORMAL_TEXT", bullet=False, suggested=False):
    if len(runs) == 1 and isinstance(runs[0], str):
        runs = (T(runs[0]),)
    atoms = []
    for kind, val, style in runs:
        if kind == "t":
            for ch in val:
                atoms.append({"k": "c", "ch": ch, "st": dict(style), "sg": suggested})
        else:
            atoms.append({"k": "o", "kind": val})
    atoms.append({"k": "c", "ch": "\n", "st": {}, "sg": False})
    return {"type": "p", "atoms": atoms, "named": named, "bullet": bullet}


def TABLE(*rows):
    return {"type": "table", "rows": [list(r) for r in rows]}


def DOC(*body, headers=(), footers=(), footnotes=(), extra_tabs=()):
    tab = {
        "id": "t.0",
        "title": "Tab 1",
        "body": list(body),
        "headers": {f"kix.h{i}": [h] for i, h in enumerate(headers)},
        "footers": {f"kix.f{i}": [h] for i, h in enumerate(footers)},
        "footnotes": {f"kix.n{i}": [h] for i, h in enumerate(footnotes)},
    }
    tabs = [tab]
    for i, (title, blocks) in enumerate(extra_tabs, 1):
        tabs.append(
            {
                "id": f"t.{i}",
                "title": title,
                "body": list(blocks),
                "headers": {},
                "footers": {},
                "footnotes": {},
            }
        )
    return {"rev": 1, "tabs": tabs}


def _plen(p):
    return sum(u16(a["ch"]) if a["k"] == "c" else 1 for a in p["atoms"])


def _layout(blocks, idx, out):
    """Assign indices: out gets (paragraph, start, end); returns the next index."""
    for b in blocks:
        if b["type"] == "p":
            out.append((b, idx, idx + _plen(b)))
            idx += _plen(b)
        else:
            idx += 1
            for row in b["rows"]:
                idx += 1
                for cell in row:
                    idx += 1
                    idx = _layout(cell, idx, out)
    return idx


def _para_json(p, start):
    els, idx, run = [], start, None

    def flush():
        nonlocal run
        if run is not None:
            els.append(run)
            run = None

    for a in p["atoms"]:
        if a["k"] == "c":
            n = u16(a["ch"])
            key = (json.dumps(a["st"], sort_keys=True), a["sg"])
            if run is not None and run["_key"] == key:
                run["textRun"]["content"] += a["ch"]
                run["endIndex"] += n
            else:
                flush()
                run = {
                    "_key": key,
                    "startIndex": idx,
                    "endIndex": idx + n,
                    "textRun": {"content": a["ch"], "textStyle": copy.deepcopy(a["st"])},
                }
                if a["sg"]:
                    run["textRun"]["suggestedInsertionIds"] = ["sugg.1"]
            idx += n
        else:
            flush()
            member = {
                "footnote": {"footnoteReference": {"footnoteId": "kix.n0"}},
                "image": {"inlineObjectElement": {"inlineObjectId": "kix.img"}},
                "person": {"person": {"personId": "p1"}},
            }[a["kind"]]
            els.append({"startIndex": idx, "endIndex": idx + 1, **member})
            idx += 1
    flush()
    for e in els:
        e.pop("_key", None)
    return {
        "paragraph": {
            "elements": els,
            "paragraphStyle": {"namedStyleType": p["named"]},
            **({"bullet": {"listId": "kix.list"}} if p["bullet"] else {}),
        }
    }


def _content_json(blocks, start):
    out, idx = [], start
    for b in blocks:
        if b["type"] == "p":
            e = _para_json(b, idx)
            e["startIndex"], e["endIndex"] = idx, idx + _plen(b)
            idx += _plen(b)
            out.append(e)
        else:
            t0 = idx
            idx += 1
            rows = []
            for row in b["rows"]:
                idx += 1
                cells = []
                for cell in row:
                    idx += 1
                    c, idx = _content_json(cell, idx)
                    cells.append({"content": c})
                rows.append({"tableCells": cells})
            out.append({"startIndex": t0, "endIndex": idx, "table": {"tableRows": rows}})
    return out, idx


def doc_json(model, doc_id):
    tabs = []
    for t in model["tabs"]:
        body, _end = _content_json(t["body"], 1)
        body = [{"startIndex": 0, "endIndex": 1, "sectionBreak": {}}, *body]
        dt = {"body": {"content": body}}
        for kind, idk in (
            ("headers", "headerId"),
            ("footers", "footerId"),
            ("footnotes", "footnoteId"),
        ):
            dt[kind] = {
                sid: {idk: sid, "content": _content_json(blocks, 0)[0]}
                for sid, blocks in t[kind].items()
            }
        tabs.append({"tabProperties": {"tabId": t["id"], "title": t["title"]}, "documentTab": dt})
    return {"documentId": doc_id, "title": "t", "revisionId": f"rev-{model['rev']}", "tabs": tabs}


def all_paragraphs(model):
    out = []
    for t in model["tabs"]:
        _layout(t["body"], 1, out)
        for k in ("headers", "footers", "footnotes"):
            for blocks in t[k].values():
                _layout(blocks, 0, out)
    return [p for p, _s, _e in out]


def ptext(p):
    return "".join(a["ch"] for a in p["atoms"] if a["k"] == "c").rstrip("\n")


def texts(model):
    return [ptext(p) for p in all_paragraphs(model) if ptext(p).strip()]


def skeleton(model):
    """Everything except the characters: tabs, segments, tables, paragraph style, lists,
    inline objects and the style of every text span (text replaced by its length class)."""

    def blk(b):
        if b["type"] == "table":
            return ["table", [[[blk(x) for x in cell] for cell in row] for row in b["rows"]]]
        spans, cur = [], None
        for a in b["atoms"]:
            if a["k"] == "o":
                spans.append(("obj", a["kind"]))
                cur = None
            elif a["ch"] != "\n":
                key = json.dumps(a["st"], sort_keys=True)
                if cur == key:
                    continue
                spans.append(("txt", key))
                cur = key
        return ["p", b["named"], b["bullet"], spans]

    return [
        [
            t["title"],
            [blk(b) for b in t["body"]],
            sorted(json.dumps([blk(x) for x in v]) for v in t["headers"].values()),
            sorted(json.dumps([blk(x) for x in v]) for v in t["footers"].values()),
            sorted(json.dumps([blk(x) for x in v]) for v in t["footnotes"].values()),
        ]
        for t in model["tabs"]
    ]


class Bad(Exception):
    def __init__(self, status, msg, reason=""):
        super().__init__(msg)
        self.status, self.msg, self.reason = status, msg, reason


# ============================================================================
# Fake Google (Drive v3 + Docs v1 + token endpoint).
# ============================================================================
class FakeGoogle:
    def __init__(self, *, page_size=200, shared_drive=True, impersonate=None):
        self.files: dict = {}
        self.n = 0
        self.clock = 0
        self.page_size = page_size
        self.tokens: set = set()
        self.mints = 0
        self.log: list = []
        self.writes: list = []  # (kind, file id) of every SUCCESSFUL mutating request
        self.doc_reads: list = []
        self.stray_hosts: list = []
        self.fail: list = []
        self.shared = {SRC, TGT}
        self.writable = {TGT}
        self.shared_drive = shared_drive
        self.impersonate = impersonate
        self.deny_trash = False
        self.deny_share = False
        self.incomplete = False
        self.bad_key = False
        self.before_batch = None
        self.token_ttl = 3600
        self._mk(ROOT_ID, "root", FOLDER_MIME, None)
        self._mk(SRC, "Source", FOLDER_MIME, ROOT_ID)
        self._mk(TGT, "Translations", FOLDER_MIME, ROOT_ID)
        self._mk(OTHER, "Other", FOLDER_MIME, ROOT_ID)

    # ---- model -----------------------------------------------------------------
    def _stamp(self):
        self.clock += 1
        return f"2026-10-10T10:{self.clock // 60:02d}:{self.clock % 60:02d}.000Z"

    def _id(self):
        self.n += 1
        return f"FILE{self.n:06d}xxxxxxxxxxxxxx"

    def _mk(self, fid, name, mime, parent, model=None):
        self.files[fid] = {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "parents": [parent] if parent else [],
            "trashed": False,
            "modifiedTime": self._stamp(),
            "version": "1",
            "model": model,
        }
        return self.files[fid]

    def _folder(self, parent, name, create=True):
        for f in self.files.values():
            if f["parents"] == [parent] and f["name"] == name and f["mimeType"] == FOLDER_MIME:
                return f
        return self._mk(self._id(), name, FOLDER_MIME, parent) if create else None

    def add_doc(self, path, model, root=SRC):
        parts = path.split("/")
        parent = root
        for p in parts[:-1]:
            parent = self._folder(parent, p)["id"]
        return self._mk(self._id(), parts[-1], DOC_MIME, parent, copy.deepcopy(model))

    def add_other(self, path, mime, root=SRC):
        parts = path.split("/")
        parent = root
        for p in parts[:-1]:
            parent = self._folder(parent, p)["id"]
        return self._mk(self._id(), parts[-1], mime, parent)

    def path(self, fid):
        out, f = [], self.files.get(fid)
        while f and f["id"] != ROOT_ID:
            out.append(f["name"])
            f = self.files.get(f["parents"][0]) if f["parents"] else None
        return "/".join(reversed(out))

    def find(self, path, root=None):
        for f in self.files.values():
            if (
                self.path(f["id"]) == path
                and not f["trashed"]
                and (root is None or self._under(f["id"], root))
            ):
                return f
        return None

    def docs_under(self, root, include_trashed=False):
        return sorted(
            self.path(f["id"])
            for f in self.files.values()
            if f["mimeType"] == DOC_MIME
            and self._under(f["id"], root)
            and (include_trashed or not f["trashed"])
        )

    def _under(self, fid, root):
        f = self.files.get(fid)
        while f:
            if f["id"] == root:
                return True
            f = self.files.get(f["parents"][0]) if f["parents"] else None
        return False

    def visible(self, fid):
        f = self.files.get(fid)
        while f:
            if f["id"] in self.shared:
                return True
            f = self.files.get(f["parents"][0]) if f["parents"] else None
        return False

    def is_writable(self, fid):
        return any(self._under(fid, w) for w in self.writable)

    def human_edit(self, fid, new_text):
        """A person types into the Doc: text, revision and modifiedTime all move."""
        f = self.files[fid]
        for p in all_paragraphs(f["model"]):
            if ptext(p).strip():
                p["atoms"] = [{"k": "c", "ch": c, "st": {}, "sg": False} for c in new_text] + [
                    {"k": "c", "ch": "\n", "st": {}, "sg": False}
                ]
                break
        f["model"]["rev"] += 1
        f["modifiedTime"] = self._stamp()

    def edit_source(self, fid, model):
        f = self.files[fid]
        f["model"] = copy.deepcopy(model)
        f["model"]["rev"] = 1000 + self.clock
        f["modifiedTime"] = self._stamp()

    def touch(self, fid):
        """A metadata-only change (rename, share): modifiedTime moves, content does not."""
        self.files[fid]["modifiedTime"] = self._stamp()
        self.files[fid]["version"] = str(int(self.files[fid]["version"]) + 1)

    def inject(self, method, contains, status, *, times=1, json_body=None, headers=None):
        self.fail.append(
            {
                "method": method,
                "contains": contains,
                "status": status,
                "times": times,
                "json": json_body,
                "headers": headers or {},
            }
        )

    def transport(self):
        return httpx.MockTransport(self._handle)

    def invalidate_tokens(self):
        self.tokens.clear()

    # ---- dispatch ----------------------------------------------------------------
    @staticmethod
    def _resp(status, body=None, headers=None):
        return httpx.Response(status, json=body if body is not None else {}, headers=headers or {})

    def _err(self, status, msg="x", reason="", rpc=""):
        body = {"error": {"code": status, "message": msg, "errors": [{"reason": reason}]}}
        if rpc:
            body["error"]["status"] = rpc
        return self._resp(status, body)

    def _handle(self, req):
        host = req.url.host
        self.log.append((req.method, host, unquote(req.url.path), req.url.query.decode()))
        for f in self.fail:
            if f["times"] > 0 and f["method"] == req.method and f["contains"] in str(req.url):
                f["times"] -= 1
                return httpx.Response(
                    f["status"],
                    json=f["json"] or {"error": {"code": f["status"], "message": "injected"}},
                    headers=f["headers"],
                )
        try:
            if host == TOKEN_H:
                return self._token(req)
            if host not in (DRIVE_H, DOCS_H):
                self.stray_hosts.append(host)
                return httpx.Response(599)
            auth = req.headers.get("authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
                return self._err(401, "Invalid Credentials", "authError", "UNAUTHENTICATED")
            if host == DRIVE_H:
                return self._drive(req)
            return self._docs(req)
        except Bad as b:
            return self._err(b.status, b.msg, b.reason)

    def _token(self, req):
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return self._resp(400, {"error": "unsupported_grant_type"})
        try:
            claims = jwt.decode(
                form["assertion"],
                PUB,
                algorithms=["RS256"],
                audience=TOKEN_URL,
                options={"require": ["exp", "iat", "iss", "scope", "aud"]},
            )
        except Exception as e:
            return self._resp(
                400, {"error": "invalid_grant", "error_description": f"Invalid JWT: {e}"}
            )
        hdr = jwt.get_unverified_header(form["assertion"])
        if (
            claims["iss"] != SA_EMAIL
            or claims["scope"] != "https://www.googleapis.com/auth/drive"
            or claims["exp"] - claims["iat"] > 3600
            or hdr.get("kid") != KEY_ID
            or self.bad_key
        ):
            # a real error never echoes the key; this one echoes the assertion to prove scrubbing
            return self._resp(
                400,
                {
                    "error": "invalid_grant",
                    "error_description": f"Invalid JWT Signature. {form['assertion']}",
                },
            )
        if (claims.get("sub") or None) != self.impersonate:
            return self._resp(
                401, {"error": "unauthorized_client", "error_description": "Client is unauthorized"}
            )
        self.mints += 1
        tok = f"ya29.fixture-token-{self.mints}-abcdef"
        self.tokens.add(tok)
        return self._resp(
            200, {"access_token": tok, "expires_in": self.token_ttl, "token_type": "Bearer"}
        )

    # ---- drive -------------------------------------------------------------------
    def _json(self, f):
        d = {k: v for k, v in f.items() if k not in ("model", "perms")}
        d["capabilities"] = {"canAddChildren": self.is_writable(f["id"]), "canEdit": True}
        return d

    def _get_visible(self, fid):
        f = self.files.get(fid)
        if f is None or not self.visible(fid):
            raise Bad(404, f"File not found: {fid}.", "notFound")
        return f

    def _need_all_drives(self, q):
        if (q.get("supportsAllDrives") or [""])[0] != "true":
            raise Bad(404, "File not found (supportsAllDrives missing)", "notFound")

    def _quota(self, parent_id, mime):
        if mime != FOLDER_MIME and not self.shared_drive and self.impersonate is None:
            raise Bad(
                403,
                "The user's Drive storage quota has been exceeded.",
                "storageQuotaExceeded",
            )

    def _drive(self, req):
        path, q = unquote(req.url.path), parse_qs(req.url.query.decode())
        self._need_all_drives(q)
        m = re.fullmatch(r"/drive/v3/files(?:/(?P<id>[^/]+)(?P<tail>/copy|/permissions)?)?", path)
        if not m:
            return self._err(404, "unhandled", "notFound")
        fid, tail, meth = m["id"], m["tail"], req.method
        body = json.loads(req.content) if req.content else {}
        if fid is None and meth == "GET":
            return self._list(q)
        if fid is None and meth == "POST":
            parent = (body.get("parents") or [""])[0]
            self._get_visible(parent)
            if not self.is_writable(parent):
                raise Bad(403, "insufficient permissions", "forbidden")
            f = self._mk(self._id(), body["name"], body["mimeType"], parent)
            self.writes.append(("create-folder", f["id"]))
            return self._resp(200, self._json(f))
        f = self._get_visible(fid)
        if meth == "GET" and not tail:
            return self._resp(200, self._json(f))
        if tail == "/permissions" and meth == "GET":
            return self._resp(200, {"permissions": copy.deepcopy(f.get("perms") or [])})
        if tail == "/permissions" and meth == "POST":
            mail = (q.get("sendNotificationEmail") or [None])[0]
            if body.get("type") in ("user", "group") and mail != "false":
                raise Bad(400, "the fake insists on sendNotificationEmail=false", "invalid")
            if body.get("type") not in ("user", "group") and mail is not None:
                raise Bad(400, "sendNotificationEmail only for users and groups", "invalid")
            if not self.is_writable(fid) or self.deny_share:
                raise Bad(403, "insufficient permissions to share", "forbidden")
            f.setdefault("perms", []).append({**body, "id": f"perm{len(f.get('perms') or [])}"})
            self.writes.append(("share", fid))
            return self._resp(200, {"id": "perm"})
        if meth == "POST" and tail:
            parent = (body.get("parents") or [""])[0]
            self._get_visible(parent)
            if not self.is_writable(parent):
                raise Bad(403, "insufficient permissions", "forbidden")
            self._quota(parent, f["mimeType"])
            model = copy.deepcopy(f["model"])
            for i, t in enumerate(model["tabs"]):
                t["id"] = f"t.c{i}"
                for k, pre in (("headers", "h"), ("footers", "f"), ("footnotes", "n")):
                    t[k] = {f"kix.c{pre}{len(t[k]) - j}": v for j, v in enumerate(t[k].values())}
            model["rev"] = 1
            c = self._mk(self._id(), body["name"], f["mimeType"], parent, model)
            self.writes.append(("copy", c["id"]))
            return self._resp(200, self._json(c))
        if meth == "PATCH":
            if not self.is_writable(fid):
                raise Bad(403, "The user does not have sufficient permissions", "forbidden")
            if body.get("trashed") and self.deny_trash:
                raise Bad(403, "insufficient permissions to trash", "forbidden")
            if "name" in body:
                f["name"] = body["name"]
            if "trashed" in body:
                f["trashed"] = bool(body["trashed"])
            f["modifiedTime"] = self._stamp()
            self.writes.append(("patch", fid))
            return self._resp(200, self._json(f))
        return self._err(404, "unhandled", "notFound")

    def _list(self, q):
        if (q.get("includeItemsFromAllDrives") or [""])[0] != "true":
            raise Bad(400, "includeItemsFromAllDrives required", "badRequest")
        expr = (q.get("q") or [""])[0]
        m = re.match(r"'([^']+)' in parents and trashed = false", expr)
        if not m:
            raise Bad(400, "Invalid Value", "invalid")
        mime = re.search(r" and mimeType = '([^']+)'", expr)
        name = re.search(r" and name = '((?:[^'\\]|\\.)*)'$", expr)
        wanted = re.sub(r"\\(.)", r"\1", name.group(1)) if name else None
        if not self.visible(m.group(1)):
            return self._resp(200, {"files": []})
        rows = [
            f
            for f in self.files.values()
            if f["parents"] == [m.group(1)]
            and not f["trashed"]
            and (not mime or f["mimeType"] == mime.group(1))
            and (wanted is None or f["name"] == wanted)
        ]
        rows.sort(key=lambda f: f["name"].lower())
        size = min(int((q.get("pageSize") or [self.page_size])[0]), self.page_size)
        start = int((q.get("pageToken") or [0])[0])
        out = {"files": [self._json(f) for f in rows[start : start + size]]}
        if self.incomplete:
            out["incompleteSearch"] = True
        if start + size < len(rows):
            out["nextPageToken"] = str(start + size)
        return self._resp(200, out)

    # ---- docs --------------------------------------------------------------------
    def _docs(self, req):
        path, q = unquote(req.url.path), parse_qs(req.url.query.decode())
        m = re.fullmatch(r"/v1/documents/(?P<id>[^/:]+)(?P<act>:batchUpdate)?", path)
        if not m:
            return self._err(404, "unhandled", "notFound")
        f = self._get_visible(m["id"])
        if f["mimeType"] != DOC_MIME:
            raise Bad(
                400, "This operation is not supported for this document", "failedPrecondition"
            )
        if req.method == "GET" and not m["act"]:
            if (q.get("includeTabsContent") or [""])[0] != "true" or (
                q.get("suggestionsViewMode") or [""]
            )[0] != "SUGGESTIONS_INLINE":
                raise Bad(400, "includeTabsContent / SUGGESTIONS_INLINE required", "invalid")
            self.doc_reads.append(f["id"])
            return self._resp(200, doc_json(f["model"], f["id"]))
        if req.method == "POST" and m["act"]:
            if not self.is_writable(f["id"]):
                raise Bad(403, "The caller does not have permission", "forbidden")
            if self.before_batch:
                hook, self.before_batch = self.before_batch, None
                hook(f["id"])
            body = json.loads(req.content)
            wc = body.get("writeControl") or {}
            if (
                "requiredRevisionId" in wc
                and wc["requiredRevisionId"] != f"rev-{f['model']['rev']}"
            ):
                raise Bad(
                    400,
                    "Invalid requests: the required revision ID does not match the latest revision",
                    "invalid",
                )
            work = copy.deepcopy(f["model"])
            for r in body["requests"]:
                self._apply(work, r)
            work["rev"] += 1
            f["model"] = work
            f["modifiedTime"] = self._stamp()
            self.writes.append(("batch", f["id"]))
            return self._resp(
                200,
                {
                    "documentId": f["id"],
                    "writeControl": {"requiredRevisionId": f"rev-{work['rev']}"},
                },
            )
        return self._err(404, "unhandled", "notFound")

    @staticmethod
    def _segment(model, tab_id, seg_id):
        tab = (
            next((t for t in model["tabs"] if t["id"] == tab_id), None)
            if tab_id
            else model["tabs"][0]
        )
        if tab is None:
            raise Bad(400, "Invalid tab id", "invalid")
        if not seg_id:
            return tab["body"], 1
        for k in ("headers", "footers", "footnotes"):
            if seg_id in tab[k]:
                return tab[k][seg_id], 0
        raise Bad(400, f"Invalid segment id {seg_id}", "invalid")

    def _locate(self, model, loc_or_range, a, b=None):
        blocks, start = self._segment(
            model, loc_or_range.get("tabId"), loc_or_range.get("segmentId")
        )
        out = []
        _layout(blocks, start, out)
        for p, s, e in out:
            if s <= a and (b if b is not None else a) <= e - 1:
                return p, s
        raise Bad(400, f"Invalid index {a}", "invalid")

    @staticmethod
    def _atom_at(p, off):
        pos = 0
        for i, a in enumerate(p["atoms"]):
            if pos == off:
                return i
            pos += u16(a["ch"]) if a["k"] == "c" else 1
        raise Bad(400, "index inside a character", "invalid")

    def _apply(self, model, r):
        ((name, args),) = r.items()
        if name == "insertText":
            loc = args["location"]
            if "\n" in args["text"]:
                raise Bad(400, "this fake does not model paragraph splits", "invalid")
            if loc.get("index", 0) < 1 and not loc.get("segmentId"):
                raise Bad(400, "Invalid insertion index (section break)", "invalid")
            p, s = self._locate(model, loc, loc["index"])
            i = self._atom_at(p, loc["index"] - s)
            ref = p["atoms"][i - 1] if i > 0 else p["atoms"][0]
            st = copy.deepcopy(ref.get("st", {})) if ref["k"] == "c" else {}
            p["atoms"][i:i] = [
                {"k": "c", "ch": c, "st": copy.deepcopy(st), "sg": False} for c in args["text"]
            ]
        elif name == "deleteContentRange":
            rg = args["range"]
            a, b = rg["startIndex"], rg["endIndex"]
            p, s = self._locate(model, rg, a, b)
            if b > s + _plen(p) - 1:
                raise Bad(400, "range includes the paragraph's final newline", "invalid")
            i, j = self._atom_at(p, a - s), self._atom_at(p, b - s)
            del p["atoms"][i:j]
        elif name == "updateTextStyle":
            rg = args["range"]
            a, b = rg["startIndex"], rg["endIndex"]
            fields = [x for x in args["fields"].split(",") if x]
            if not fields or any(x not in STYLE_FIELDS for x in fields):
                raise Bad(400, f"Invalid fields {args['fields']}", "invalid")
            p, s = self._locate(model, rg, a, b)
            i, j = self._atom_at(p, a - s), self._atom_at(p, b - s)
            for atom in p["atoms"][i:j]:
                if atom["k"] != "c":
                    continue
                for fld in fields:
                    if fld in args["textStyle"]:
                        atom["st"][fld] = copy.deepcopy(args["textStyle"][fld])
                    else:
                        atom["st"].pop(fld, None)
        else:
            raise Bad(400, f"unsupported request {name}", "invalid")


# ============================================================================
# helpers
# ============================================================================
BASE_CFG = {
    "source_folder": SRC,
    "target_folder": TGT,
    "source_locale": "en-US",
    "target_locales": "fr-FR",
}


def _cfg(extra=None):
    return {**BASE_CFG, **(extra or {})}


def _conn(name, extra=None, secret=None):
    return cstore.create(
        "gdocs", name, _cfg(extra), secret or {"service_account_json": SA_JSON}, actor="admin"
    )


def _mc(extra=None, fake=None, secret=None):
    fake = fake or FakeGoogle()
    c = GoogleDocsConnector(
        _cfg(extra), secret or {"service_account_json": SA_JSON}, transport=fake.transport()
    )
    return c, fake


def _run(cid, fake, **kw):
    return cstore.gdocs_translate(cid, actor="auto", transport=fake.transport(), **kw)


def _cfgval(cid, key):
    with Session(engine) as s:
        return dict((s.get(Connection, cid).config or {}).get(key) or {})


def _hashes(cid):
    return _cfgval(cid, "gdocs_hashes")


def _raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except cbase.ConnectorError:
        return True
    return False


def _needs_review(mod, only_sources=None):
    from sqlmodel import select

    from app.models import Segment, SegStatus

    orig = mod.pretranslate_document

    def patched(s, doc_id, loc, *a, **kw):
        out = orig(s, doc_id, loc, *a, **kw)
        for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if only_sources is None or sg.source_text in only_sources:
                sg.status = SegStatus.NEEDS_REVIEW
                s.add(sg)
        s.commit()
        return out

    return orig, patched


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _outside_target(fake):
    return [w for w in fake.writes if not fake._under(w[1], TGT)]


def rich_doc():
    return DOC(
        P("Welcome to the product", named="HEADING_1"),
        P(
            T("Click "),
            T("here", bold=True, link={"url": "https://example.com/start"}),
            T(" to get started today."),
        ),
        P("First item", bullet=True),
        P("Second item", bullet=True),
        P(T("The sentence before the note"), OBJ("footnote"), T(" and the sentence after it.")),
        TABLE(
            [[P("Cell one text")], [P("Cell two text")]],
            [[P("Cell three text")], [P("Cell four text")]],
        ),
        P(OBJ("image")),
        P("   "),
        P(T("Emoji \U0001f600 inside the text stays aligned")),
        headers=[P("Company confidential")],
        footers=[P("Page footer words")],
        footnotes=[P("The footnote words here.")],
        extra_tabs=[("Tab Two", [P("Second tab body text")])],
    )


def main() -> None:
    from app.web._routes import walk_routes

    init_db()
    cap = _Capture()
    logging.getLogger().addHandler(cap)
    logging.getLogger().setLevel(logging.DEBUG)
    import app.services.translate as T_

    # ---- (1) capability, validators, test() -----------------------------------------
    assert GoogleDocsConnector.capability.topology == "T3"
    mc, fk = _mc()
    fk.add_doc("a", DOC(P("Hello")))
    ok, msg = mc.test()
    assert ok, msg
    assert fk.mints == 1 and not fk.stray_hosts and not fk.writes
    # a folder URL is as good as an id; ids are validated
    assert parse_folder_ref(f"https://drive.google.com/drive/folders/{SRC}?usp=sharing", "x") == SRC
    assert parse_folder_ref(f"https://drive.google.com/drive/u/0/folders/{SRC}", "x") == SRC
    bad_ids = [
        "",
        "root",
        "..",
        "../x",
        "%2e%2e",
        "a b",
        "x" * 9,
        "x" * 129,
        "abc'def1234567",
        "abc\r\ndef1234567",
        "abc\x00def1234567",
        "abc?x=1def1234",
        "abc#def12345678",
        "abc/def12345678",
        "١٢٣٤٥٦٧٨٩١",
        f"{SRC}/..",
    ]
    for b in bad_ids:
        assert _raises(validate_file_id, b), ("id refused", b)
    for b in (
        "http://drive.google.com/drive/folders/" + SRC,
        "https://evil.example/drive/folders/" + SRC,
        "https://drive.google.com.evil.example/drive/folders/" + SRC,
        "https://user:pw@drive.google.com/drive/folders/" + SRC,
        "https://drive.google.com/drive/my-drive",
        "ftp://drive.google.com/drive/folders/" + SRC,
    ):
        assert _raises(parse_folder_ref, b, "x"), ("url refused", b)
    print(f"  A: {len(bad_ids)} hostile ids and 6 hostile folder URLs refused")

    # ---- (2) operator config + key validation, fail-closed (taxonomy A) ---------------
    key_bad = [
        "",
        "not json",
        "[]",
        json.dumps({"type": "authorized_user", "client_email": SA_EMAIL, "private_key": PEM}),
        json.dumps(
            {"type": "service_account", "client_email": "a@evil.example", "private_key": PEM}
        ),
        json.dumps({"type": "service_account", "client_email": SA_EMAIL, "private_key": "nope"}),
        json.dumps({"type": "service_account", "client_email": SA_EMAIL}),
    ]
    for kb in key_bad:
        mcb, fkb = _mc(secret={"service_account_json": kb})
        ok, msg = mcb.test()
        assert not ok and not fkb.log, ("bad key refused, nothing sent", kb[:30], msg)
    bad_cfgs = [
        {"source_folder": ""},
        {"source_folder": ".."},
        {"source_folder": "root"},
        {"source_folder": "a/../b" + "x" * 10},
        {"source_folder": "x'y" + "z" * 12},
        {"target_folder": ""},
        {"target_folder": "../x"},
        {"target_folder": SRC},
        {"impersonate_user": "../x"},
        {"impersonate_user": "a b@example.com"},
        {"impersonate_user": "x@"},
    ]
    for extra in bad_cfgs:
        mcb, fkb = _mc(extra)
        ok, msg = mcb.test()
        assert not ok, ("refused", extra, msg)
        assert not fkb.writes and not fkb.log, ("nothing sent for a bad config", extra, fkb.log)
        assert _raises(mcb.discover_articles), ("discovery refuses", extra)
    # overlap, checked with the network: target inside source, source inside target, not a folder
    for extra, fn in (
        ({}, lambda f: f._mk(TGT, "Translations", FOLDER_MIME, SRC)),
        ({}, lambda f: f._mk(SRC, "Source", FOLDER_MIME, TGT)),
    ):
        fko = FakeGoogle()
        fn(fko)
        ok, msg = _mc(extra, fake=fko)[0].test()
        assert not ok and "overlap" in msg, msg
    fko = FakeGoogle()
    fko.add_doc("a", DOC(P("Hello")))
    docid = fko.find("Source/a")["id"]
    ok, msg = _mc({"source_folder": docid}, fake=fko)[0].test()
    assert not ok and "folders" in msg, msg
    ok, msg = _mc({"source_folder": "1Unshared00000000000000"}, fake=fko)[0].test()
    assert not ok and SA_EMAIL in msg, ("tells the operator which email to share with", msg)
    fko.writable = set()
    ok, msg = _mc(fake=fko)[0].test()
    assert not ok and "cannot add files" in msg, msg
    print(
        f"  A: {len(key_bad)} bad keys + {len(bad_cfgs)} hostile configs refused before any request; overlap / unshared / read-only target refused"
    )

    # ---- (3) auth: JWT claims, remint, DWD sub, scrub -----------------------------------
    fk3 = FakeGoogle()
    fk3.add_doc("a", DOC(P("Hello")))
    mc3, _ = _mc(fake=fk3)
    assert mc3.test()[0] and fk3.mints == 1
    mc3.discover_articles()
    assert fk3.mints == 1, "the token is cached"
    fk3.invalidate_tokens()
    assert mc3.discover_articles() and fk3.mints == 2, "401 -> one remint -> retried"
    assert fk3.log[0][1] == TOKEN_H and fk3.log[0][2] == "/token", fk3.log[0]
    assert not [x for x in fk3.log if x[1] not in (TOKEN_H, DRIVE_H, DOCS_H)]
    fkd = FakeGoogle(impersonate="alex@example.com")
    fkd.add_doc("a", DOC(P("Hello")))
    assert _mc({"impersonate_user": "alex@example.com"}, fake=fkd)[0].test()[0], "sub claim sent"
    ok, msg = _mc(fake=fkd)[0].test()
    assert not ok and "domain-wide delegation" not in msg and "unauthorized_client" in msg, msg
    ok, msg = _mc({"impersonate_user": "alex@example.com"}, fake=fk3)[0].test()
    assert not ok and "domain-wide delegation" in msg, msg
    fk3.bad_key = True
    fk3.invalidate_tokens()
    ok, msg = _mc(fake=fk3)[0].test()
    assert not ok and "sign-in failed" in msg, msg
    for leak in (PEM_BODY, KEY_ID, "eyJ"):
        assert leak not in msg, ("H: scrubbed", leak)
    blob = "\n".join(cap.lines)
    assert PEM_BODY not in blob and KEY_ID not in blob and "ya29." not in blob
    print(
        "  auth: RS256 JWT (iss/scope/aud/exp/kid) verified by the fake, cached, one remint on 401, sub only when set, key/assertion scrubbed"
    )

    # ---- (4) D/E: discovery ---------------------------------------------------------------
    fk4 = FakeGoogle(page_size=2)
    for i in range(5):
        fk4.add_doc(f"f{i}", DOC(P(f"Text {i}")))
    fk4.add_doc("Sub/Deep/x", DOC(P("Hello deep")))
    fk4.add_other("sheet", "application/vnd.google-apps.spreadsheet")
    fk4.add_other("slides", "application/vnd.google-apps.presentation")
    fk4.add_other("file.pdf", "application/pdf")
    fk4.add_other("shortcut", SHORTCUT_MIME)
    fk4.add_doc("tab\tname", DOC(P("Control char name")))
    gone = fk4.add_doc("trashed", DOC(P("In the trash")))
    gone["trashed"] = True
    fk4.add_doc("Private", DOC(P("Private")), root=OTHER)
    fk4.add_doc("fr-FR/old", DOC(P("Not source")), root=TGT)
    mc4, _ = _mc(fake=fk4)
    arts = mc4.discover_articles()
    rels = sorted(a["relpath"] for a in arts)
    assert rels == sorted([f"f{i}" for i in range(5)] + ["Sub/Deep/x"]), rels
    assert not fk4.doc_reads, "discovery reads no document"
    sk = mc4.sel.skipped_dict()
    for name, why in [
        ("sheet", "Sheets"),
        ("slides", "Slides"),
        ("file.pdf", "PDF"),
        ("shortcut", "shortcut"),
        ("tab\tname", "control character"),
    ]:
        assert name in sk and why in sk[name], (name, sk.get(name))
    assert "trashed" not in sk and not any("Private" in a["relpath"] for a in arts)
    assert "not a Google Doc" in mc4.sel.selection_summary(), "loud skip leads the summary"
    top, _ = _mc({"include_subfolders": "0"}, fake=fk4)
    assert all("/" not in a["relpath"] for a in top.discover_articles())
    fk5 = FakeGoogle()
    for i in range(20):
        fk5.add_doc(f"A/a{i:02d}", DOC(P(f"a{i}")))
    for i in range(3):
        fk5.add_doc(f"B/b{i}", DOC(P(f"b{i}")))
        fk5.add_doc(f"C/c{i}", DOC(P(f"c{i}")))
    got = _mc({"max_files": "9"}, fake=fk5)[0].discover_articles()
    per = {}
    for a in got:
        per[a["rel"][0]] = per.get(a["rel"][0], 0) + 1
    assert len(got) == 9 and per == {"A": 3, "B": 3, "C": 3}, ("E: distribution", per)
    print(
        f"  D: only Google Docs selected, paging + recursion, skips named with a reason; E: cap 9 over 20/3/3 -> {per}"
    )

    # ---- (5) A: explicit ids are scope-checked -----------------------------------------------
    fk6 = FakeGoogle()
    inside = fk6.add_doc("Sub/in", DOC(P("Inside")))
    top_doc = fk6.add_doc("top", DOC(P("Top")))
    foreign = fk6.add_doc("secret", DOC(P("Foreign")), root=OTHER)
    in_target = fk6.add_doc("fr-FR/copy", DOC(P("Target")), root=TGT)
    sheet = fk6.add_other("sheet", "application/vnd.google-apps.spreadsheet")
    folder = fk6._folder(SRC, "Sub")
    trashed = fk6.add_doc("tr", DOC(P("tr")))
    trashed["trashed"] = True
    mc6, _ = _mc(fake=fk6)
    ok_arts = mc6.discover_articles(article_ids=[inside["id"], top_doc["id"]])
    assert sorted(a["relpath"] for a in ok_arts) == ["Sub/in", "top"], ok_arts
    for label, bad in (
        ("foreign (unshared) doc", foreign["id"]),
        ("doc in the target folder", in_target["id"]),
        ("a spreadsheet", sheet["id"]),
        ("the source folder itself", SRC),
        ("a sub-folder", folder["id"]),
        ("unknown id", "1DoesNotExist000000000"),
    ):
        fk6.writes.clear()
        n_reads = len(fk6.doc_reads)
        try:
            res = _mc(fake=fk6)[0].discover_articles(article_ids=[bad])
            refused = not res
        except cbase.ConnectorError:
            refused = True
        assert refused, ("refused", label)
        assert not fk6.writes and len(fk6.doc_reads) == n_reads, label
    for label in ("..", "../x", "%2e%2e", "a'b" + "c" * 10, "x\r\nyyyyyyyyyyyy", "root"):
        assert _raises(_mc(fake=fk6)[0].discover_articles, article_ids=[label]), label
    # a doc in ANOTHER folder that is also shared to the service account is still out of scope
    fk6.shared.add(OTHER)
    peer = fk6.add_doc("peer", DOC(P("Peer")), root=OTHER)
    n_reads = len(fk6.doc_reads)
    assert _raises(_mc(fake=fk6)[0].discover_articles, article_ids=[peer["id"]]), (
        "shared but outside"
    )
    assert len(fk6.doc_reads) == n_reads
    fk6.shared.discard(OTHER)
    mc6b, _ = _mc(fake=fk6)
    assert mc6b.discover_articles(article_ids=[trashed["id"]]) == [] and mc6b.fetch_notes, "trashed"
    print(
        "  A: explicit ids - inside ok; foreign, unshared, target-side, non-Doc, folder, unknown, trashed and 6 hostile strings refused, 0 writes, 0 reads"
    )

    # ---- (6) full flow: fidelity, tree, nothing outside the target ---------------------------
    fk7 = FakeGoogle()
    rich = fk7.add_doc("Guides/Intro", rich_doc())
    plain = fk7.add_doc("plain", DOC(P("Hello world"), P("Second paragraph")))
    src_models = {f["id"]: copy.deepcopy(f["model"]) for f in fk7.files.values() if f["model"]}
    src_stamps = {f["id"]: f["modifiedTime"] for f in fk7.files.values()}
    c7 = _conn("flow", {"target_locales": "fr-FR, pt-br"})
    ok, msg, s7 = _run(c7.id, fk7)
    assert ok, msg
    assert s7["created"] == 4 and s7["updated"] == 0 and not s7["failures"], s7
    assert fk7.docs_under(TGT) == sorted(
        f"Translations/{loc}/{p}" for loc in ("fr-FR", "pt-BR") for p in ("Guides/Intro", "plain")
    ), fk7.docs_under(TGT)
    cp = fk7.find("Translations/fr-FR/Guides/Intro")
    assert not cp["name"].startswith(PENDING_PREFIX), "finished name"
    assert skeleton(cp["model"]) == skeleton(src_models[rich["id"]]), (
        "FIDELITY: tabs, headers, footers, footnotes, tables, lists, objects and every text style kept"
    )
    src_txt, new_txt = texts(src_models[rich["id"]]), texts(cp["model"])
    assert (
        len(src_txt) == len(new_txt)
        and all(n.lower().startswith("[fr") or n.lower().startswith("[fr-fr") for n in new_txt)
    ) or all("fr" in n.lower() for n in new_txt), new_txt
    assert new_txt != src_txt
    # the link + bold span survived on the right characters
    linked = [
        "".join(a["ch"] for a in p["atoms"] if a["k"] == "c" and a["st"].get("link"))
        for p in all_paragraphs(cp["model"])
    ]
    assert any(ln for ln in linked), ("the hyperlink survived translation", linked)
    for p in all_paragraphs(cp["model"]):
        for a in p["atoms"]:
            if a["k"] == "c" and a["st"].get("link"):
                assert a["st"]["link"] == {"url": "https://example.com/start"} and a["st"]["bold"]
    # headers, footers, footnotes and the second tab were translated too
    assert (
        any("confidential" not in t.lower() for t in new_txt)
        and "Company confidential" not in new_txt
    )
    assert "Second tab body text" not in new_txt and "The footnote words here." not in new_txt
    assert {
        f["id"]: f["model"]
        for f in fk7.files.values()
        if f["id"] in src_models and fk7._under(f["id"], SRC)
    } == {i: m for i, m in src_models.items() if fk7._under(i, SRC)}, "source models untouched"
    assert all(
        fk7.files[i]["modifiedTime"] == t for i, t in src_stamps.items() if fk7._under(i, SRC)
    ), "source never written"
    assert not _outside_target(fk7), (
        "every write is under the target folder",
        _outside_target(fk7),
    )
    assert not fk7.stray_hosts
    rows = idmap.list_for_connection(c7.id)
    assert len(rows) == 4 and all(r.status == idmap.DONE and r.target_id for r in rows), rows
    assert len(_hashes(c7.id)) == 4
    ids7 = {r.target_id for r in rows}
    assert ids7 == {
        fk7.find(f"Translations/{loc}/{p}")["id"]
        for loc in ("fr-FR", "pt-BR")
        for p in ("Guides/Intro", "plain")
    }
    folders_before = sorted(f["name"] for f in fk7.files.values() if f["mimeType"] == FOLDER_MIME)
    print(
        "  flow: 2 docs x 2 locales -> mirrored per-locale Docs; tabs/headers/footers/footnotes/tables/lists/objects/styles identical to the source; link + bold kept; source untouched"
    )

    # ---- (7) C: steady state ------------------------------------------------------------------
    fk7.doc_reads.clear()
    fk7.writes.clear()
    n_log = len(fk7.log)
    ok, msg, s7b = _run(c7.id, fk7)
    assert ok and s7b["created"] == 0 and s7b["updated"] == 0 and s7b["skipped"] == 4, s7b
    assert not fk7.doc_reads and not fk7.writes, (
        "C: nothing read or written",
        fk7.doc_reads,
        fk7.writes,
    )
    assert not [x for x in fk7.log[n_log:] if x[0] in ("POST", "PATCH") and x[1] != TOKEN_H]
    assert (
        sorted(f["name"] for f in fk7.files.values() if f["mimeType"] == FOLDER_MIME)
        == folders_before
    )
    print("  C: run 2 over unchanged sources -> 4 skipped, 0 document reads, 0 writes")
    # a rename / share touch of the source: one read, no MT, no write
    calls = []
    real_pre = T_.pretranslate_document

    def counting(*a, **kw):
        calls.append(1)
        return real_pre(*a, **kw)

    fk7.touch(plain["id"])
    set_pieces("app.services.translate", "pretranslate_document", counting)
    try:
        ok, msg, s7c = _run(c7.id, fk7)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_pre)
    assert s7c["skipped"] == 4 and not calls and not fk7.writes, (
        "metadata touch is not a change",
        s7c,
    )
    assert fk7.doc_reads == [plain["id"]], ("re-read once, to compare the digest", fk7.doc_reads)
    fk7.doc_reads.clear()
    ok, msg, _s7d = _run(c7.id, fk7)
    assert not fk7.doc_reads, (
        "the new modifiedTime is remembered: run after the touch reads nothing"
    )
    # a text-only content edit re-translates only that Doc and rewrites it IN PLACE: the same file
    # id (readers' links, sharing and comments survive), one atomic batch, nothing trashed
    old_fr = fk7.find("Translations/fr-FR/plain")["id"]
    fk7.edit_source(plain["id"], DOC(P("Hello world CHANGED"), P("Second paragraph")))
    fk7.writes.clear()
    ok, msg, s7e = _run(c7.id, fk7)
    assert ok and s7e["updated"] == 2 and s7e["skipped"] == 2 and s7e["created"] == 0, s7e
    new_fr = fk7.find("Translations/fr-FR/plain")
    assert new_fr["id"] == old_fr and not new_fr["trashed"], "same file id: updated in place"
    assert [k for k, _ in fk7.writes if k in ("copy", "create-folder")] == [], fk7.writes
    assert not any(f["trashed"] for f in fk7.files.values() if f["mimeType"] == DOC_MIME), (
        "nothing was trashed"
    )
    assert "CHANGED" in " ".join(texts(new_fr["model"])), texts(new_fr["model"])
    assert not new_fr["name"].startswith(PENDING_PREFIX)
    assert sum(1 for p in fk7.docs_under(TGT) if p.endswith("fr-FR/plain")) == 1, (
        "exactly one live copy"
    )
    row = idmap.get(c7.id, plain["id"], "fr-fr")
    assert row.status == idmap.DONE and row.target_id == old_fr, "the id map keeps the id"
    assert not _outside_target(fk7)
    assert fk7.doc_reads.count(rich["id"]) == 0, "the unchanged Doc was not read"
    print(
        "  C: a rename/share touch re-reads once with no MT and no write; a content edit re-translates only that Doc IN PLACE (same file id, no copy, nothing trashed)"
    )

    # ---- (8) B/F: held-back never creates or changes a Doc -----------------------------------------
    fk8 = FakeGoogle()
    h = fk8.add_doc("h", DOC(P("Keep me"), P("And me")))
    c8 = _conn("held")
    orig, patched = _needs_review(T_)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s8 = _run(c8.id, fk8)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert (
        s8["created"] == 0 and s8["updated"] == 0 and s8["held_back"] == 1 and not s8["failures"]
    ), s8
    assert not fk8.docs_under(TGT, include_trashed=True), "B: no copy, not even a pending one"
    assert not [w for w in fk8.writes if w[0] in ("copy", "batch")], fk8.writes
    assert not _hashes(c8.id), "F: no hash for a held pair"
    ok, msg, s8b = _run(c8.id, fk8)
    assert ok and s8b["created"] == 1, ("F: a healthy re-run completes it", s8b)
    # PARTIAL hold on an EXISTING copy
    fk8.edit_source(h["id"], DOC(P("Keep me NEW"), P("And me")))
    before = copy.deepcopy(fk8.find("Translations/fr-FR/h")["model"])
    before_id = fk8.find("Translations/fr-FR/h")["id"]
    h_before = dict(_hashes(c8.id))
    orig, patched = _needs_review(T_, only_sources={"And me"})
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        fk8.writes.clear()
        ok, msg, s8c = _run(c8.id, fk8)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    cur = fk8.find("Translations/fr-FR/h")
    assert cur["id"] == before_id and cur["model"] == before and not fk8.writes, (
        "B: a partial hold leaves the stored Doc exactly as it was, nothing written"
    )
    assert s8c["updated"] == 0 and s8c["held_back"] == 1 and _hashes(c8.id) == h_before, s8c
    ok, msg, s8d = _run(c8.id, fk8)
    assert (
        ok
        and s8d["updated"] == 1
        and "NEW" in " ".join(texts(fk8.find("Translations/fr-FR/h")["model"]))
    )
    # a NEW locale variant is not created while a unit is held
    c8b = _conn("held2", {"target_locales": "fr-FR, de-DE"})
    orig, patched = _needs_review(T_, only_sources={"And me"})
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        _run(c8b.id, fk8)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert not fk8.find("Translations/de-DE/h"), "B: no partially translated variant is created"
    print(
        "  B/F: all-held -> no Doc at all; partial hold on a new AND an existing Doc -> not written, copy intact, no hash; healthy re-run completes"
    )

    # ---- (9) B: a Doc edited by hand is never overwritten -------------------------------------------
    fk9 = FakeGoogle()
    e = fk9.add_doc("e", DOC(P("Edit target")))
    c9 = _conn("edited")
    assert _run(c9.id, fk9)[2]["created"] == 1
    mine = fk9.find("Translations/fr-FR/e")
    fk9.human_edit(mine["id"], "Version corrigee par un humain")
    human = copy.deepcopy(mine["model"])
    fk9.edit_source(e["id"], DOC(P("Edit target v2")))
    calls.clear()
    set_pieces("app.services.translate", "pretranslate_document", counting)
    try:
        fk9.writes.clear()
        ok, msg, s9 = _run(c9.id, fk9)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_pre)
    assert mine["model"] == human and not fk9.writes and not calls, (
        "zero writes and ZERO MT spend",
        fk9.writes,
        calls,
    )
    assert s9["edited_copies"] == [{"article_id": e["id"], "locale": "fr-fr"}], s9
    assert "edited in Google Docs" in msg and ok, msg
    # the person trashes the copy -> it is regenerated
    mine["trashed"] = True
    ok, msg, s9b = _run(c9.id, fk9)
    regen = fk9.find("Translations/fr-FR/e")
    assert ok and regen and regen["id"] != mine["id"] and "v2" in " ".join(texts(regen["model"])), (
        s9b
    )
    # unchanged source + deleted copy is also regenerated (the probe reads the copy)
    regen["trashed"] = True
    ok, msg, s9c = _run(c9.id, fk9)
    assert fk9.find("Translations/fr-FR/e") and s9c["skipped"] == 0, s9c
    # the switch: protect_edited_copies=0 overwrites (by building a fresh copy)
    c9b = _conn("edited-off", {"protect_edited_copies": "0"})
    fk9b = FakeGoogle()
    e2 = fk9b.add_doc("e", DOC(P("Edit target")))
    _run(c9b.id, fk9b)
    fk9b.human_edit(fk9b.find("Translations/fr-FR/e")["id"], "human")
    fk9b.edit_source(e2["id"], DOC(P("Edit target v2")))
    ok, msg, s9d = _run(c9b.id, fk9b)
    assert s9d["updated"] == 1, ("switch off: the protection is off", s9d)
    # the revision race: a person edits between our read and our write -> 400, nothing overwritten
    fk9c = FakeGoogle()
    e3 = fk9c.add_doc("r", DOC(P("Race me")))
    c9c = _conn("race")
    fk9c.before_batch = lambda fid: fk9c.human_edit(fid, "typed meanwhile")
    ok, msg, s9e = _run(c9c.id, fk9c)
    pend = [
        f for f in fk9c.files.values() if f["mimeType"] == DOC_MIME and fk9c._under(f["id"], TGT)
    ]
    assert s9e["failures"] and "changed while" in str(s9e["failures"]), s9e
    assert all("typed meanwhile" in " ".join(texts(f["model"])) for f in pend), (
        "the person's text kept"
    )
    assert pend and all(f["name"].startswith(PENDING_PREFIX) for f in pend), "still marked pending"
    assert idmap.get(c9c.id, e3["id"], "fr-fr").status == idmap.BUILDING
    # the write-time check alone (the pre-check is bypassed, as in a race): still refuses
    fk9d = FakeGoogle()
    e4 = fk9d.add_doc("w", DOC(P("Write time")))
    c9d = _conn("writetime")
    _run(c9d.id, fk9d)
    mine4 = fk9d.find("Translations/fr-FR/w")
    fk9d.human_edit(mine4["id"], "texte humain")
    kept4 = copy.deepcopy(mine4["model"])
    fk9d.edit_source(e4["id"], DOC(P("Write time v2")))
    real_parked = GoogleDocsConnector.parked
    GoogleDocsConnector.parked = lambda self, aid, loc, chash: False
    try:
        fk9d.writes.clear()
        ok, msg, s9f = _run(c9d.id, fk9d)
    finally:
        GoogleDocsConnector.parked = real_parked
    assert mine4["model"] == kept4 and not mine4["trashed"] and not fk9d.writes, (
        "B: the write-time check refuses on its own",
        fk9d.writes,
    )
    assert "changed in Google Docs" in (msg + json.dumps(s9f["failures"])), msg
    # a corrupted id map that points at a SOURCE Doc is never written through
    fk9e = FakeGoogle()
    e5 = fk9e.add_doc("m", DOC(P("Map attack")))
    c9e = _conn("badmap")
    idmap.reserve(c9e.id, e5["id"], "fr-fr", e5["id"], status=idmap.DONE)
    fk9e.edit_source(e5["id"], DOC(P("Map attack v2")))
    ok, msg, s9g = _run(c9e.id, fk9e)
    assert not [
        x for x in fk9e.log if x[0] in ("PATCH", "POST") and x[1] == DRIVE_H and e5["id"] in x[2]
    ]
    assert not [w for w in fk9e.writes if w[1] == e5["id"]], "the source Doc was never written"
    assert s9g["failures"] and "not inside the target folder" in json.dumps(s9g["failures"]), s9g
    print(
        "  B: hand-edited Doc untouched (0 writes, 0 MT), trash-to-regenerate, switch off works, requiredRevisionId race loses safely"
    )

    # ---- (10) interrupted builds resume; no duplicate Docs ----------------------------------------------
    fk10 = FakeGoogle()
    fk10.add_doc("i", DOC(P("Interrupt me"), P("Twice")))
    c10 = _conn("resume")
    fk10.inject("POST", ":batchUpdate", 500, times=1)
    ok, msg, s10 = _run(c10.id, fk10)
    assert s10["failures"], s10
    pend = [
        f for f in fk10.files.values() if f["mimeType"] == DOC_MIME and fk10._under(f["id"], TGT)
    ]
    assert len(pend) == 1 and pend[0]["name"].startswith(PENDING_PREFIX), "a visibly pending copy"
    assert texts(pend[0]["model"]) == ["Interrupt me", "Twice"], "atomic: nothing half-applied"
    pid = pend[0]["id"]
    ok, msg, s10b = _run(c10.id, fk10)
    assert ok and s10b["created"] == 1, s10b
    live = [
        f
        for f in fk10.files.values()
        if f["mimeType"] == DOC_MIME and fk10._under(f["id"], TGT) and not f["trashed"]
    ]
    assert len(live) == 1 and live[0]["id"] == pid and live[0]["name"] == "i", (
        "resumed in place, renamed"
    )
    # the write landed but the rename failed -> next run finds it applied and only renames
    fk10b = FakeGoogle()
    fk10b.add_doc("j", DOC(P("Rename me")))
    c10b = _conn("rename")
    fk10b.inject("PATCH", "/files/", 500, times=3)
    ok, msg, s10c = _run(c10b.id, fk10b)
    assert s10c["failures"], s10c
    pend = [
        f for f in fk10b.files.values() if f["mimeType"] == DOC_MIME and fk10b._under(f["id"], TGT)
    ]
    assert (
        len(pend) == 1
        and pend[0]["name"].startswith(PENDING_PREFIX)
        and "Rename me" not in texts(pend[0]["model"])
    )
    batches = [w for w in fk10b.writes if w[0] == "batch"]
    ok, msg, s10d = _run(c10b.id, fk10b)
    assert ok and s10d["created"] == 1, s10d
    assert [w for w in fk10b.writes if w[0] == "batch"] == batches, "NOT applied a second time"
    assert [f["name"] for f in pend] == ["j"]
    print(
        "  resume: a failed batch leaves one visible pending copy that the next run completes in place; a failed rename is finished without re-applying the edits"
    )

    # ---- (11) vendor errors, quota, rate limits ----------------------------------------------------------
    fk11 = FakeGoogle(shared_drive=False)
    fk11.add_doc("q", DOC(P("Quota")))
    ok, msg, s11 = _run(_conn("quota").id, fk11)
    assert not ok or s11["failures"], s11
    blob = msg + json.dumps(s11["failures"])
    assert "storage" in blob and "shared drive" in blob and "Impersonate" in blob, blob
    assert not fk11.docs_under(TGT), "no Doc left behind"
    fk11d = FakeGoogle(shared_drive=False, impersonate="alex@example.com")
    fk11d.add_doc("q", DOC(P("Quota")))
    ok, msg, s11d = _run(_conn("dwd", {"impersonate_user": "alex@example.com"}).id, fk11d)
    assert ok and s11d["created"] == 1, (msg, s11d)
    fk11u = FakeGoogle()
    fk11u.add_doc("u", DOC(P("Unshared")))
    fk11u.shared = {SRC}
    fk11u.writable = set()
    ok, msg = _mc(fake=fk11u)[0].test()
    assert not ok and SA_EMAIL in msg, msg
    # rate limit: 403 userRateLimitExceeded is retried once after a pause; 429 via base._request
    fk12 = FakeGoogle()
    fk12.add_doc("r", DOC(P("Rate")))
    sleeps = []
    real_sleep = cbase._sleep
    cbase._sleep = lambda s: sleeps.append(s)
    try:
        fk12.inject(
            "GET",
            "/drive/v3/files",
            403,
            times=1,
            json_body={
                "error": {
                    "code": 403,
                    "message": "User rate limit exceeded",
                    "errors": [{"reason": "userRateLimitExceeded"}],
                }
            },
        )
        assert _mc(fake=fk12)[0].test()[0] and sleeps, "403 userRateLimitExceeded -> backoff -> ok"
        sleeps.clear()
        fk12.inject("GET", "/drive/v3/files", 429, times=1, headers={"Retry-After": "2"})
        mc12, _ = _mc(fake=fk12)
        assert mc12.discover_articles() and sleeps == [2.0], sleeps
    finally:
        cbase._sleep = real_sleep
    fk12.inject("GET", "/v1/documents", 403, times=1)
    ok, msg, s12 = _run(_conn("denied-doc").id, fk12)
    assert s12.get("unreadable") == 1 and "could not read" in msg, (msg, s12)
    # trash denied (Contributor role): the update still lands, with a warning
    fk13 = FakeGoogle()
    t13 = fk13.add_doc("t", DOC(P("Trash")))
    c13 = _conn("notrash")
    _run(c13.id, fk13)
    fk13.edit_source(
        t13["id"], DOC(P("Trash v2"), P("A new paragraph"))
    )  # structure change: rebuilt
    fk13.deny_trash = True
    ok, msg, s13 = _run(c13.id, fk13)
    assert ok and s13["updated"] == 1 and "trash" in msg and s13["warnings"], (msg, s13)
    print(
        "  vendor: storageQuotaExceeded explained (shared drive / Impersonate), DWD copy ok, unshared folder names the email, 403 rate limit backoff + 429 Retry-After, unreadable doc isolated, trash denied warns"
    )

    # ---- (12) pre-existing human Doc fails closed; suggestions; size ---------------------------------------
    fk14 = FakeGoogle()
    fk14.add_doc("p", DOC(P("Mine")))
    fk14.add_doc("fr-FR/p", DOC(P("Traduit par un humain")), root=TGT)
    human14 = copy.deepcopy(fk14.find("Translations/fr-FR/p")["model"])
    c14 = _conn("preexisting")
    ok, msg, s14 = _run(c14.id, fk14)
    assert s14["created"] == 0 and "needs_link" in (msg + json.dumps(s14["failures"])), (msg, s14)
    assert fk14.find("Translations/fr-FR/p")["model"] == human14 and not fk14.writes
    fk15 = FakeGoogle()
    fk15.add_doc("s", DOC(P("Pending suggestion", suggested=True)))
    fk15.add_doc("ok", DOC(P("Fine")))
    ok, msg, s15 = _run(_conn("sugg").id, fk15)
    assert s15["created"] == 1 and s15["unreadable"] == 1 and "suggestions" in msg, (msg, s15)
    assert fk15.docs_under(TGT) == ["Translations/fr-FR/ok"]
    fk16 = FakeGoogle()
    fk16.add_doc("big", DOC(*[P(f"Para {i}") for i in range(12)]))
    fk16.add_doc("empty", DOC(P("   "), P(OBJ("image"))))
    set_pieces("app.services.connectors.gdocs", "MAX_UNITS_PER_DOC", 10)
    try:
        ok, msg, s16 = _run(_conn("big").id, fk16)
    finally:
        set_pieces("app.services.connectors.gdocs", "MAX_UNITS_PER_DOC", 1500)
    assert s16["unreadable"] == 1 and s16["empty_skipped"] == 1 and not fk16.docs_under(TGT), (
        msg,
        s16,
    )
    print(
        "  D: a human-made Doc at the copy's place -> needs_link, untouched; pending suggestions / oversize skipped with a reason; an empty Doc skipped"
    )

    # ---- (13) folder safety: shortcut or Doc posing as a locale folder -------------------------------------------
    fk17 = FakeGoogle()
    fk17.add_doc("d", DOC(P("Folder games")))
    fk17._mk(fk17._id(), "fr-FR", SHORTCUT_MIME, TGT)  # a shortcut named like the locale folder
    fk17._mk(fk17._id(), "fr-FR", DOC_MIME, TGT, DOC(P("A Doc named fr-FR")))
    ok, msg, s17 = _run(_conn("shortcut").id, fk17)
    assert ok and s17["created"] == 1, (msg, s17)
    made = fk17.find("Translations/fr-FR/d")
    parent = fk17.files[made["parents"][0]]
    assert parent["mimeType"] == FOLDER_MIME and parent["parents"] == [TGT], (
        "a real new folder was made"
    )
    assert not _outside_target(fk17)
    # a hostile locale never becomes a folder name
    fk18 = FakeGoogle()
    fk18.add_doc("d", DOC(P("Locales")))
    ok, msg, _s18 = _run(_conn("locales", {"target_locales": "fr-FR, ../x, de-DE"}).id, fk18)
    assert "Rejected" in msg and "../x" in msg and ok, msg
    assert fk18.docs_under(TGT) == ["Translations/de-DE/d", "Translations/fr-FR/d"], (
        fk18.docs_under(TGT)
    )
    ok, msg, _s18b = _run(_conn("srcloc", {"target_locales": "en-US, fr-FR"}).id, fk18)
    assert "equal to the source locale" in msg, msg
    ok, msg, _ = _run(_conn("nosrc", {"source_locale": ""}).id, FakeGoogle())
    assert not ok and "Source locale" in msg, msg
    print(
        "  A: a shortcut or Doc named like a locale folder is never used as one; hostile locale rejected; source locale dropped; missing source locale fails loud"
    )

    # ---- (14) H: WriteBudget, egress guard, hosts, secrets in no log ---------------------------------------------
    saved = getattr(app_config, "MAX_WRITES_PER_RUN", None)
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        fk19 = FakeGoogle()
        fk19.add_doc("a", DOC(P("A")))
        fk19.add_doc("b", DOC(P("B")))
        ok, msg, s19 = _run(_conn("budget", {"target_locales": "fr-FR, de-DE"}).id, fk19)
        assert ok and s19["ceiling_hit"] and s19["created"] + s19["updated"] == 1, s19
        assert len(fk19.docs_under(TGT)) == 1, ("exactly one Doc written", fk19.docs_under(TGT))
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    from app.services.connectors import egress_guard

    fk20 = FakeGoogle()
    fk20.add_doc("a", DOC(P("A")))
    real = egress_guard.egress_is_real
    egress_guard.egress_is_real = lambda t: True
    try:
        ok, msg, _s = _run(_conn("egress").id, fk20)
    finally:
        egress_guard.egress_is_real = real
    assert not ok and not fk20.writes and not fk20.doc_reads, ("mock MT + real egress blocked", msg)
    mc21, _ = _mc(fake=FakeGoogle())
    for url in (
        "https://evil.example/v1/documents/x",
        "https://www.googleapis.com.evil.example/drive/v3/files",
        "http://docs.googleapis.com/v1/documents/x",
        "/etc/passwd",
    ):
        assert _raises(mc21._google_url, url), url
    print(
        "  H: WriteBudget MAX_WRITES_PER_RUN=1 -> exactly one Doc; egress guard refuses mock output; only googleapis.com URLs are built"
    )

    # ---- (15) credential pin: scope edits clear the key ----------------------------------------------------------------
    c22 = _conn("pin")
    cfg22 = dict(cstore.get(c22.id).config)
    cstore.update(c22.id, "pin", {**cfg22, "max_files": "10"}, {"service_account_json": ""})
    assert cstore.get(c22.id).secret_enc and _has_credential(cstore.get(c22.id)), (
        "benign edit keeps the key"
    )
    cstore.update(
        c22.id,
        "pin",
        {**cfg22, "max_files": "10", "source_folder": OTHER},
        {"service_account_json": ""},
    )
    assert not _has_credential(cstore.get(c22.id)), "moving the source folder clears the stored key"
    c22b = _conn("pin-target")
    cstore.update(
        c22b.id,
        "pin-target",
        {**dict(cstore.get(c22b.id).config), "target_folder": OTHER},
        {"service_account_json": ""},
    )
    assert not _has_credential(cstore.get(c22b.id)), (
        "moving the target folder clears the stored key"
    )
    c23 = _conn("pin2")
    cstore.update(
        c23.id,
        "pin2",
        {**dict(cstore.get(c23.id).config), "impersonate_user": "bob@example.com"},
        {"service_account_json": ""},
    )
    assert not _has_credential(cstore.get(c23.id)), (
        "changing the delegated user clears the stored key"
    )
    c24 = _conn("pin3")
    cstore.update(
        c24.id,
        "pin3",
        {
            **dict(cstore.get(c24.id).config),
            "source_folder": f"https://drive.google.com/drive/folders/{SRC}",
        },
        {"service_account_json": ""},
    )
    assert _has_credential(cstore.get(c24.id)), "the same folder as a URL is not a move"
    print(
        "  credential pin: editing a folder or the delegated user clears the stored key; max_files / a URL form of the same folder do not"
    )

    # ---- (16) pull(), retry, units ---------------------------------------------------------------------------------------
    fk25 = FakeGoogle()
    fk25.add_doc("a", DOC(P("Pull me"), P("And me")))
    mc25, _ = _mc(fake=fk25)
    items = mc25.pull()
    assert [i.text for i in items] == ["Pull me", "And me"], items
    assert _raises(mc25.push, items)
    fk26 = FakeGoogle()
    fk26.add_doc("a", DOC(P("Retry me")))
    c26 = _conn("retry")
    fk26.inject("POST", ":batchUpdate", 503, times=1)
    ok, msg, s26 = _run(c26.id, fk26)
    assert s26["failures"], s26
    ok, msg, s26b = cstore.retry_failed(
        c26.id, s26["failures"], actor="auto", transport=fk26.transport()
    )
    assert ok and fk26.find("Translations/fr-FR/a"), (msg, s26b)
    # parser unit checks: UTF-16 indices, whitespace edges kept, soft breaks, non-text elements split units
    d = DOC(
        P(T("  Padded "), OBJ("footnote"), T("tail\u000bnext line")), P("Emoji \U0001f600 tail")
    )
    units, _problems = parse_units(doc_json(d, "x"))
    assert [u.plain for u in units] == ["Padded", "tail\nnext line", "Emoji \U0001f600 tail"], [
        u.plain for u in units
    ]
    assert units[0].start == 3 and units[0].end == 9, (units[0].start, units[0].end)
    print(
        "  pull() lists units; push refuses; retry_failed re-attempts exactly the failed pair; parser keeps edge whitespace, splits units at non-text elements"
    )

    # ---- (16b) review: the key is stored encrypted as validated one-line JSON, never a path ------------------------------------
    from app.services.connectors.gdocs import SPEC as GDOCS_SPEC

    impersonation_error = cstore.gdocs_impersonation_error

    pretty = json.dumps(json.loads(SA_JSON), indent=2)
    flat = pretty.replace("\n", "")  # a single-line password input drops the line breaks
    mini = json.dumps(json.loads(SA_JSON), separators=(",", ":"))
    stored_keys = []
    for label, pasted in (("pretty", pretty), ("flat", flat), ("mini", mini)):
        ck = cstore.create("gdocs", "key-" + label, _cfg(), {"service_account_json": pasted})
        stored_keys.append(cstore.dec_secret(cstore.get(ck.id).secret_enc)["service_account_json"])
        assert "service_account_json" not in json.dumps(cstore.get(ck.id).config), "never in config"
    assert stored_keys[0] == stored_keys[1] == stored_keys[2], "one canonical stored form"
    assert "\n" not in stored_keys[0] and stored_keys[0].startswith('{"type":"service_account"')
    stored = json.loads(stored_keys[0])
    assert stored["token_uri"] == TOKEN_URL and stored["client_email"] == SA_EMAIL
    assert set(stored) <= {
        "type",
        "token_uri",
        "project_id",
        "private_key_id",
        "private_key",
        "client_email",
        "client_id",
    }
    mck, _fck = _mc(secret={"service_account_json": stored_keys[0]})
    assert mck.test()[0], "the canonical form signs and connects"
    # a raw line break inside the private_key string (a client that unescaped it) still parses
    raw_nl = pretty.replace("\\n", "\n")
    assert (
        cstore.dec_secret(
            cstore.create("gdocs", "key-rawnl", _cfg(), {"service_account_json": raw_nl}).secret_enc
        )["service_account_json"]
        == stored_keys[0]
    )
    # there is no path field: nothing on the spec, and a path in the config reads nothing
    names = {f.name for f in GDOCS_SPEC.config_fields + GDOCS_SPEC.secret_fields}
    assert not {n for n in names if "path" in n or n.startswith("key_file")}, names
    secret_path = os.path.join(_ROOT, "pyproject.toml")
    fp = GoogleDocsConnector(
        {**_cfg(), "key_file_path": secret_path, "service_account_json": secret_path}, {}
    )
    okp, msgp = fp.test()
    assert not okp and "required" in msgp and "[tool" not in msgp, msgp
    # invalid keys are refused at every writer WITHOUT echoing what was pasted
    evil = json.loads(SA_JSON)
    evil["token_uri"] = "https://evil.example/token"
    not_sa = {**json.loads(SA_JSON), "type": "authorized_user"}
    no_pem = {**json.loads(SA_JSON), "private_key": "hunter2-not-a-pem-marker"}
    bad_mail = {**json.loads(SA_JSON), "client_email": "x@evil.example"}
    for label, bad in (
        ("evil token_uri", json.dumps(evil)),
        ("authorized_user", json.dumps(not_sa)),
        ("no pem", json.dumps(no_pem)),
        ("bad client_email", json.dumps(bad_mail)),
        ("a file path", "C:\\Users\\alex\\secret-key.json"),
        ("a posix path", "/etc/passwd"),
        ("garbage", "hunter2-" + "x" * 40),
        ("oversize", "{" + " " * 20000 + "}"),
    ):
        try:
            cstore.create("gdocs", "bad", _cfg(), {"service_account_json": bad})
        except cbase.ConnectorError as exc:
            for frag in ("hunter2", "evil.example", "secret-key", "/etc/passwd", PEM_BODY[:30]):
                assert frag not in str(exc), (label, "the error echoed the paste")
        else:
            raise AssertionError(f"a bad key was stored: {label}")
        okr, msgr = cstore.rotate_secret(
            _ck_id := cstore.create("gdocs", "rot", _cfg(), {"service_account_json": mini}).id,
            {"service_account_json": bad},
            transport=FakeGoogle().transport(),
        )
        assert not okr and "hunter2" not in msgr and "evil.example" not in msgr, (label, msgr)
        assert (
            cstore.dec_secret(cstore.get(_ck_id).secret_enc)["service_account_json"]
            == stored_keys[0]
        ), "a refused rotate keeps the stored key"
        assert _raises(cstore.update, _ck_id, "rot", {}, {"service_account_json": bad}), label
    from app.web.api import _find_secret_fields

    assert _find_secret_fields({"config": {"service_account_json": "x"}}) == [
        "service_account_json"
    ], "the /api create and update bodies refuse a key smuggled into config"
    assert _find_secret_fields({"config": {"private_key": "x"}}) == ["private_key"]
    print(
        "  key: pretty / flattened / minified paste -> one canonical encrypted one-line JSON; token_uri pinned to Google; no path field; refusals never echo the paste, a refused rotate keeps the key"
    )

    # ---- (16c) review: impersonation is admin-only and off by default ------------------------------------------------------------
    assert impersonation_error("gdocs", {}, {"impersonate_user": "a@x.com"}, caller_is_admin=False)
    assert not impersonation_error(
        "gdocs", {}, {"impersonate_user": "a@x.com"}, caller_is_admin=True
    )
    assert not impersonation_error(
        "gdocs",
        {"impersonate_user": "a@x.com"},
        {"impersonate_user": "A@x.com"},
        caller_is_admin=False,
    ), "an unchanged value (a form re-submit) is allowed"
    assert impersonation_error(
        "gdocs",
        {"impersonate_user": "a@x.com"},
        {"impersonate_user": "b@x.com"},
        caller_is_admin=False,
    )
    assert not impersonation_error(
        "gdocs", {"impersonate_user": "a@x.com"}, {"impersonate_user": ""}, caller_is_admin=False
    ), "clearing only narrows"
    assert not impersonation_error("gdocs", {}, {"max_files": "5"}, caller_is_admin=False)
    assert not impersonation_error(
        "jira", {}, {"impersonate_user": "a@x.com"}, caller_is_admin=False
    )
    assert not str(_cfg().get("impersonate_user") or ""), "off by default"
    assert (
        "ADMIN ONLY"
        in next(f for f in GDOCS_SPEC.config_fields if f.name == "impersonate_user").help
    )
    print(
        "  impersonate_user: only an admin can set or change it; clearing and re-submits are fine"
    )

    # ---- (16d) review: update IN PLACE keeps the link, sharing and structure ---------------------------------------------------------
    def retext(model, old, new):
        for p in all_paragraphs(model):
            if ptext(p) == old:
                p["atoms"] = [{"k": "c", "ch": ch, "st": {}, "sg": False} for ch in new] + [
                    {"k": "c", "ch": "\n", "st": {}, "sg": False}
                ]
                return
        raise AssertionError(old)

    def seg_text(model, needle):
        t = model["tabs"][0]
        for sid, blocks in t["footnotes"].items():
            if needle in " ".join(ptext(p) for p in blocks):
                return sid
        return None

    fkp = FakeGoogle()
    rdoc = fkp.add_doc("Guides/Rich", rich_doc())
    cpl = _conn("inplace")
    ok, msg, sp0 = _run(cpl.id, fkp)
    assert ok and sp0["created"] == 1, (msg, sp0)
    tgt = fkp.find("Translations/fr-FR/Guides/Rich")
    tid = tgt["id"]
    tgt["permissions"] = ["anyone:reader"]  # a reader link, set by a person
    new_src = copy.deepcopy(rdoc["model"])
    retext(new_src, "Company confidential", "Company secret")
    retext(new_src, "The footnote words here.", "The footnote words CHANGED.")
    retext(new_src, "First item", "First item v2")
    fkp.edit_source(rdoc["id"], new_src)
    fkp.writes.clear()
    ok, msg, sp1 = _run(cpl.id, fkp)
    assert ok and sp1["updated"] == 1 and not sp1["failures"], (msg, sp1)
    cur = fkp.files[tid]
    assert not cur["trashed"] and fkp.find("Translations/fr-FR/Guides/Rich")["id"] == tid, "same id"
    assert cur["permissions"] == ["anyone:reader"], "sharing kept"
    assert [k for k, _ in fkp.writes] == ["batch"], (
        "one atomic batch, no copy, no trash",
        fkp.writes,
    )
    assert skeleton(cur["model"]) == skeleton(new_src), "structure and styles kept in place"
    joined = " ".join(texts(cur["model"]))
    assert "CHANGED" in joined and "secret" in joined and "v2" in joined, joined
    assert idmap.get(cpl.id, rdoc["id"], "fr-fr").target_id == tid
    # unchanged again -> nothing at all
    fkp.writes.clear()
    assert _run(cpl.id, fkp)[2]["skipped"] == 1 and not fkp.writes
    # in-place mapping follows the stable SEGMENT IDS, not how segments sort by text
    fks = FakeGoogle()
    two = fks.add_doc(
        "two",
        DOC(P("Body text"), footnotes=[P("Apple note"), P("Zebra note")], headers=[P("Head")]),
    )
    cs = _conn("segorder")
    _run(cs.id, fks)
    ts = fks.find("Translations/fr-FR/two")
    apple_seg = seg_text(ts["model"], "Apple")
    swapped = copy.deepcopy(two["model"])
    swapped["tabs"][0]["footnotes"] = {
        "kix.n0": swapped["tabs"][0]["footnotes"]["kix.n1"],
        "kix.n1": swapped["tabs"][0]["footnotes"]["kix.n0"],
    }
    fks.edit_source(two["id"], swapped)
    fks.writes.clear()
    ok, msg, ss1 = _run(cs.id, fks)
    assert ok and ss1["updated"] == 1 and [k for k, _ in fks.writes] == ["batch"], (msg, fks.writes)
    cur_seg = "".join(ptext(p) for p in ts["model"]["tabs"][0]["footnotes"][apple_seg])
    assert "Zebra" in cur_seg and "Apple" not in cur_seg, (
        "source footnote n0 now says Zebra, so the copy's matching footnote does too",
        cur_seg,
    )
    # a STRUCTURE change (a new paragraph) cannot be done in place: rebuilt, with the old one
    # trashed only AFTER the id map names the replacement, and a warning that the link changed
    fkr = FakeGoogle()
    rs = fkr.add_doc("r", DOC(P("Alpha"), P("Beta")))
    cr = _conn("rebuild")
    _run(cr.id, fkr)
    old_id = fkr.find("Translations/fr-FR/r")["id"]
    fkr.edit_source(rs["id"], DOC(P("Alpha"), P("Beta"), P("Gamma is new")))
    seen_at_trash = []
    real_trash = GoogleDocsConnector._trash

    def watching_trash(self, c, file_id):
        row = idmap.get(cr.id, rs["id"], "fr-fr")
        seen_at_trash.append((file_id, row.target_id, row.status))
        return real_trash(self, c, file_id)

    GoogleDocsConnector._trash = watching_trash
    try:
        fkr.writes.clear()
        ok, msg, sr = _run(cr.id, fkr)
    finally:
        GoogleDocsConnector._trash = real_trash
    new_id = fkr.find("Translations/fr-FR/r")["id"]
    assert ok and new_id != old_id and fkr.files[old_id]["trashed"], sr
    assert next(k for k, _ in fkr.writes if k in ("copy", "batch", "patch")) == "copy"
    assert seen_at_trash == [(old_id, new_id, idmap.DONE)], seen_at_trash
    assert any("link changed" in w for w in sr["warnings"]), sr["warnings"]
    # a restyle with the SAME text units (paragraph becomes a heading) is a structure change too
    fkt = FakeGoogle()
    ts_ = fkt.add_doc("t", DOC(P("Plain one"), P("Plain two")))
    ct_ = _conn("restyle")
    _run(ct_.id, fkt)
    t_old = fkt.find("Translations/fr-FR/t")["id"]
    fkt.edit_source(ts_["id"], DOC(P("Plain one"), P("Plain two v2", named="HEADING_1")))
    ok, msg, _st = _run(ct_.id, fkt)
    t_new = fkt.find("Translations/fr-FR/t")
    assert (
        ok
        and t_new["id"] != t_old
        and skeleton(t_new["model"]) == skeleton(fkt.files[ts_["id"]]["model"])
    ), "restyled source: rebuilt so the heading style is not lost"
    # the switch: update_in_place=0 always rebuilds
    fko = FakeGoogle()
    so = fko.add_doc("o", DOC(P("One"), P("Two")))
    co = _conn("noinplace", {"update_in_place": "0"})
    _run(co.id, fko)
    o_old = fko.find("Translations/fr-FR/o")["id"]
    fko.edit_source(so["id"], DOC(P("One v2"), P("Two")))
    _run(co.id, fko)
    assert fko.find("Translations/fr-FR/o")["id"] != o_old and fko.files[o_old]["trashed"]
    # hand edit detection, second layer: the text differs although modifiedTime did not move
    fkh = FakeGoogle()
    hs = fkh.add_doc("h", DOC(P("Quiet edit"), P("Second")))
    ch = _conn("quietedit")
    _run(ch.id, fkh)
    th = fkh.find("Translations/fr-FR/h")
    retext(th["model"], ptext(all_paragraphs(th["model"])[0]), "Texte reecrit par une personne")
    th["model"]["rev"] += 1  # Docs bumped the revision; the Drive modified time did not move
    kept_h = copy.deepcopy(th["model"])
    fkh.edit_source(hs["id"], DOC(P("Quiet edit v2"), P("Second")))
    fkh.writes.clear()
    ok, msg, sh = _run(ch.id, fkh)
    assert th["model"] == kept_h and not fkh.writes and not th["trashed"], fkh.writes
    assert sh["edited_copies"] and "changed in Google Docs" in json.dumps(sh["failures"]), sh
    # a person edits between our read and our write: the in-place batch loses the race safely
    fkq = FakeGoogle()
    qs = fkq.add_doc("q", DOC(P("Race in place"), P("Second")))
    cq = _conn("raceinplace")
    _run(cq.id, fkq)
    tq = fkq.find("Translations/fr-FR/q")
    fkq.edit_source(qs["id"], DOC(P("Race in place v2"), P("Second")))
    fkq.before_batch = lambda fid: fkq.human_edit(fid, "typed meanwhile")
    ok, msg, sq = _run(cq.id, fkq)
    assert "changed while" in json.dumps(sq["failures"]), sq
    assert "typed meanwhile" in " ".join(texts(tq["model"])) and not tq["trashed"]
    # Drive says the listing is incomplete: fail closed, nothing written
    fki = FakeGoogle()
    fki.add_doc("i", DOC(P("Incomplete")))
    fki.incomplete = True
    assert _raises(_mc(fake=fki)[0].discover_articles) and not fki.writes
    # OPUS GATE: text moved into a BLANK paragraph of an unchanged structure (same shape, same
    # unit count) must not be written into the old paragraph in place: it is rebuilt instead
    fkm = FakeGoogle()
    ms = fkm.add_doc("m", DOC(P("Alpha text"), P(""), P("Gamma text")))
    cm = _conn("movedtext")
    _run(cm.id, fkm)
    m_old = fkm.find("Translations/fr-FR/m")["id"]
    fkm.edit_source(ms["id"], DOC(P(""), P("Beta text"), P("Gamma text")))
    ok, msg, sm = _run(cm.id, fkm)
    m_new = fkm.find("Translations/fr-FR/m")
    paras = [ptext(p) for p in all_paragraphs(m_new["model"])]
    assert ok and not paras[0].strip() and "Beta" in paras[1] and "Gamma" in paras[2], paras
    assert m_new["id"] != m_old and any("link changed" in w for w in sm["warnings"]), sm
    # OPUS GATE: a rebuild copies the sharing a PERSON added to the old translated Doc (no mail),
    # skipping owner / inherited / the service account; if it cannot, the old Doc is NOT trashed
    for deny in (False, True):
        fkk = FakeGoogle()
        ks = fkk.add_doc("k", DOC(P("Alpha"), P("Beta")))
        ck = _conn(f"sharing{int(deny)}")
        _run(ck.id, fkk)
        k_old = fkk.find("Translations/fr-FR/k")
        k_old["perms"] = [
            {"id": "o", "type": "user", "role": "owner", "emailAddress": "boss@example.com"},
            {"id": "w", "type": "user", "role": "writer", "emailAddress": "ed@example.com"},
            {"id": "a", "type": "anyone", "role": "reader", "allowFileDiscovery": False},
            {
                "id": "i",
                "type": "group",
                "role": "reader",
                "emailAddress": "all@example.com",
                "permissionDetails": [{"inherited": True}],
            },
            {"id": "s", "type": "user", "role": "writer", "emailAddress": SA_EMAIL},
        ]
        fkk.deny_share = deny
        fkk.edit_source(ks["id"], DOC(P("Alpha"), P("Beta"), P("Gamma is new")))
        ok, msg, sk = _run(ck.id, fkk)
        k_new = fkk.files[idmap.get(ck.id, ks["id"], "fr-fr").target_id]
        assert ok and k_new["id"] != k_old["id"], (msg, sk)
        if not deny:
            got = sorted((p["type"], p["role"], p.get("emailAddress", "")) for p in k_new["perms"])
            assert got == [("anyone", "reader", ""), ("user", "writer", "ed@example.com")], got
            assert k_old["trashed"], "sharing copied: the old Doc is trashed"
        else:
            assert not k_old["trashed"], "sharing NOT copied: the old Doc stays for its readers"
            assert any("could not be copied" in w for w in sk["warnings"]), sk["warnings"]
    # OPUS GATE: no revision id in hand -> the batch is refused before anything is sent
    cz, fkz = _mc()
    zdoc = fkz.add_doc("z", DOC(P("Zed")))
    fkz.writes.clear()
    with cz._client() as zc:
        assert _raises(cz.batch_update, zc, zdoc["id"], [{"insertText": {}}], "")
    assert not fkz.writes, fkz.writes
    print(
        "  in place: same file id + sharing + structure kept (1 batch, no copy, no trash); stable segment ids (not text order) drive the mapping; a structure change rebuilds and trashes the old one only after the id map follows; switch; quiet edit and race lose safely; incompleteSearch fails closed"
    )

    # ---- (17) H: parity surfaces ---------------------------------------------------------------------------------------------
    from app.services.connectors import REGISTRY

    assert "gdocs" in REGISTRY
    tc = TestClient(app)
    assert "Google Docs" in tc.get("/connectors").text
    typ = tc.get("/connectors/gdocs").text
    assert "Source folder" in typ and "Service account key" in typ, "type form renders"
    from app.cli.__main__ import build_parser

    assert "connector gdocs-translate" in _leaf_paths(build_parser())
    from starlette.routing import Route

    api_paths = {r.path for r in walk_routes(app) if isinstance(r, Route)}
    assert "/api/connectors/{cid}/gdocs/translate" in api_paths
    from app.services.connectors import harvest as _hv

    assert _hv._map_gdocs({"title": "doc"})["source"].get("title") == "doc"
    blob = "\n".join(cap.lines)
    assert PEM_BODY not in blob and KEY_ID not in blob and "ya29." not in blob, (
        "H: key, key id and token in no log line (whole suite)"
    )
    print(
        "  H parity: REGISTRY + /connectors tile + type form + /api route + CLI leaf + harvest title; no secret in any log line"
    )
    print("verify_gdocs OK")


def _leaf_paths(parser, prefix=""):
    import argparse

    out = set()
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        return {prefix} if prefix else set()
    for sp in subs:
        for name, subparser in sp.choices.items():
            out |= _leaf_paths(subparser, f"{prefix} {name}".strip())
    return out


if __name__ == "__main__":
    main()
