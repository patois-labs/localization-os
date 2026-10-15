"""Bitbucket Cloud connector (second provider on the shared git core): fully offline via
httpx.MockTransport.

FakeBitbucket models the parts of GitHub's REST v3 the connector relies on, with the platform's
REAL validation (taxonomy G): a ref update is fast-forward only (422 otherwise), creating an
existing ref is a 422, a tree entry must name a stored blob, a pull request needs commits
between head and base and cannot be opened twice, and a recursive tree listing is TRUNCATED
past a limit so the directory-by-directory fallback is exercised. Every request is logged, so
the safety rules can be asserted on the wire: no write ever touches the base branch, one run
is one commit, and the token only ever appears in the Authorization header.

Covers the taxonomy checklist (the connector build blueprint section 3):
  A  hostile article ids / operator paths / branch names / repos are refused BEFORE any
     request (zero wire traffic), including encoded traversal and a prefix of a real path.
  B  a held-back key is never written as a translation and never overwrites the value that
     already ships in the repository; no content hash is recorded until it is approved.
  C  the existence probe reads the branch the commit lands on, so run two over an unchanged
     source is a no-op (no commit, no pull request change).
  D  only JSON (flat/nested) and PO are accepted; anything else is refused with a clear text.
  E  every source file and locale pair is written (distribution asserted, not a total).
  F  a run that wrote nothing records no hash.
  G  the fake enforces real validation (non-fast-forward, duplicate ref, missing blob).
  H  WriteBudget ceiling, mock-egress guard, token never logged, base branch never written.
Plus: JSON nested + flat, PO with plurals, one commit per run, PR open then update, merged
PR then a fresh PR, a 422 on an existing branch, truncated-tree and PR-list pagination,
credential pin on repository change, locale styles, and the four parity surfaces.

ASCII-only prints; final line is 'verify_bitbucket OK'.
"""

# ruff: noqa: E501, UP031
import base64
import fnmatch
import hashlib
import json
import logging
import os
import re
import sys
from urllib.parse import parse_qs, quote, unquote, urlsplit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_bitbucket_")

import httpx
from sqlmodel import Session, select

from app import config as app_config
from app.db import engine, init_db
from app.models import Connection, Segment, SegStatus, SyncLog
from app.services import key_files, translate
from app.services.connectors import store as cstore
from app.services.connectors.bitbucket import BitbucketConnector

TOKEN = "ATATT3xFAKE_TOKEN_for_tests_0123456789"  # pregate: allow-secret offline fixture
REPO = "acme/site"


def _sha(*parts) -> str:
    h = hashlib.sha1()  # noqa: S324 (fake git object ids, not security)
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class FakeBitbucket:
    """An in-memory Bitbucket Cloud repository behind httpx.MockTransport.

    Models the parts of the REST 2.0 API the connector relies on, with the platform's REAL
    validation (taxonomy G): POST /src with `branch` + `parents` is a 409 unless `parents` is
    the branch tip (the atomic fast-forward), creating an existing branch is a 400, a src
    listing is paginated with an absolute `next` link and carries `attributes` (link =
    symlink, subrepository = submodule), a raw read of a symlink returns the link text, a
    pull request cannot be opened twice or without changes, a PUT may demand a title, and
    reading branch restrictions needs the admin scope (403 otherwise). There is NO call that
    moves an existing branch, exactly like the real API. Every request is logged (multipart
    bodies parsed), so the wire can be asserted: host api.bitbucket.org only, one run is one
    commit, no write touches the base branch, the token only ever appears in Authorization."""

    HOST = "api.bitbucket.org"

    def __init__(self, files=None, token=TOKEN, push=True, email=None):
        self.owner, self.name = REPO.split("/")
        self.token = token
        self.email = email
        self.push = push
        self.default_branch = "main"
        self.blobs: dict = {}
        self.trees: dict = {}  # tree sha -> {path: blob sha}
        self.commits: dict = {}  # commit sha -> {tree, parents, message}
        self.refs: dict = {}  # branch -> commit sha
        self.prs: list = []
        self.log: list = []  # (method, path, body)
        self.hosts: set = set()
        self.n_commits = 0
        self.before_commit = None  # hook(self, branch) run just before a POST /src applies
        self.noise_prs = 0
        self.pr_pages: list = []
        self.dir_pages: list = []
        self.max_pagelen = 100
        self.commit_error = False
        self.full_name = REPO  # what GET /repositories/... answers
        self.pr_fail = False  # POST /pullrequests answers 500 (the commit already landed)
        self.restrictions: list = []  # branch restriction rows the host holds
        self.admin = False  # may the token read branch restrictions?
        self.strict_put = False  # a PUT without a title is a 400
        self.evil_next = None  # a `next` link pointing elsewhere
        self.lfs: set = set()  # paths whose raw read is a 301 to the media service
        self.rate_limited = 0  # the next N requests are 429
        self.token_echo = ""  # a token-shaped string the host echoes in its error text
        self.modes: dict = {}  # path -> "link" | "subrepository" | "executable"
        root = self._make_commit({}, [], "initial")
        self.refs["main"] = root
        if files:
            self.commit_to("main", files, "seed")

    # ---------------------------------------------------------------- helpers
    def _blob(self, data: bytes) -> str:
        sha = _sha("blob", data)
        self.blobs[sha] = data
        return sha

    def _make_commit(self, flat: dict, parents: list, message: str) -> str:
        tsha = _sha("tree", json.dumps(flat, sort_keys=True))
        self.trees[tsha] = dict(flat)
        self.n_commits += 1
        csha = _sha("commit", tsha, ",".join(parents), message, self.n_commits)
        self.commits[csha] = {"tree": tsha, "parents": list(parents), "message": message}
        return csha

    def commit_to(self, branch: str, files: dict, message="edit") -> str:
        """Test helper: land files on a branch like a human push (not through the API)."""
        head = self.refs[branch]
        flat = dict(self.trees[self.commits[head]["tree"]])
        for path, data in files.items():
            flat[path] = self._blob(data if isinstance(data, bytes) else data.encode("utf-8"))
        self.refs[branch] = self._make_commit(flat, [head], message)
        return self.refs[branch]

    def files_at(self, ref: str) -> dict:
        sha = self.refs.get(ref, ref)
        flat = self.trees[self.commits[sha]["tree"]]
        return {p: self.blobs[b] for p, b in flat.items()}

    def _ancestors(self, sha: str) -> set:
        seen, todo = set(), [sha]
        while todo:
            c = todo.pop()
            if c in seen or c not in self.commits:
                continue
            seen.add(c)
            todo.extend(self.commits[c]["parents"])
        return seen

    def writes(self) -> list:
        return [(m, p, b) for m, p, b in self.log if m != "GET"]

    def transport(self):
        return httpx.MockTransport(self._handle)

    def merge_pr(self, number: int) -> None:
        """Test helper: merge the PR (the base moves to the PR head)."""
        pr = next(p for p in self.prs if p["number"] == number)
        self.refs[pr["base"]] = self.refs[pr["head"]]
        pr["state"] = "MERGED"

    @staticmethod
    def _multipart(req: httpx.Request):
        ctype = req.headers.get("content-type", "")
        mb = re.search(r"boundary=([^;]+)", ctype)
        fields, files = {}, {}
        if not mb:
            return fields, files
        boundary = ("--" + mb.group(1).strip('"')).encode()
        for part in req.content.split(boundary):
            part = part.strip(b"\r\n")
            if not part or part == b"--":
                continue
            head, _, data = part.partition(b"\r\n\r\n")
            disp = head.decode("utf-8", "replace")
            nm = re.search(r'name="([^"]*)"', disp)
            fn = re.search(r'filename="([^"]*)"', disp)
            if not nm:
                continue
            if fn is not None:
                files[nm.group(1)] = data
            else:
                fields.setdefault(nm.group(1), data.decode("utf-8"))
        return fields, files

    # ---------------------------------------------------------------- the API
    def _handle(self, req: httpx.Request) -> httpx.Response:
        url = urlsplit(str(req.url))
        self.hosts.add(url.hostname)
        path = unquote(url.path)
        body = None
        if (req.headers.get("content-type") or "").startswith("multipart/"):
            f, fl = self._multipart(req)
            body = {"fields": f, "files": fl}
        elif req.content:
            try:
                body = json.loads(req.content)
            except ValueError:
                body = {"raw": req.content.decode("utf-8", "replace")}
        self.log.append((req.method, path, body))
        if self.rate_limited > 0:
            self.rate_limited -= 1
            return httpx.Response(
                429, headers={"retry-after": "1"}, json={"error": {"message": "Rate limit"}}
            )
        if url.hostname != self.HOST or url.scheme != "https":
            return httpx.Response(404, json={"type": "error", "error": {"message": "wrong host"}})
        auth = req.headers.get("authorization", "")
        want = f"Bearer {self.token}"
        if self.email:
            want = "Basic " + base64.b64encode(f"{self.email}:{self.token}".encode()).decode()
        if auth != want:
            return httpx.Response(401, json={"type": "error", "error": {"message": "Bad token"}})
        for hdr, val in req.headers.items():
            if hdr.lower() != "authorization" and self.token in val:
                return httpx.Response(400, json={"error": {"message": "token leaked in a header"}})
        if self.token in str(req.url):
            return httpx.Response(400, json={"error": {"message": "token leaked in the url"}})
        prefix = f"/2.0/repositories/{self.owner}/{self.name}"
        if not path.startswith(prefix):
            return httpx.Response(404, json={"type": "error", "error": {"message": "Not Found"}})
        rest = path[len(prefix) :]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        m = req.method
        if rest == "" and m == "GET":
            return httpx.Response(
                200,
                json={
                    "type": "repository",
                    "full_name": self.full_name,
                    "mainbranch": {"name": self.default_branch},
                    "is_private": True,
                },
            )
        if rest == "/branch-restrictions" and m == "GET":
            if not self.admin:
                return httpx.Response(
                    403, json={"error": {"message": "Your credentials lack admin:repository"}}
                )
            return httpx.Response(200, json={"values": self.restrictions, "pagelen": 100})
        if not self.push and m != "GET":
            return httpx.Response(
                403, json={"error": {"message": "Your credentials lack write:repository"}}
            )
        mm = re.match(r"^/refs/branches/(.+)$", rest)
        if mm and m == "GET":
            b = mm.group(1)
            if b not in self.refs:
                return httpx.Response(404, json={"error": {"message": "Branch not found"}})
            return httpx.Response(
                200, json={"type": "branch", "name": b, "target": {"hash": self.refs[b]}}
            )
        if rest == "/refs/branches" and m == "POST":
            b = (body or {}).get("name", "")
            h = ((body or {}).get("target") or {}).get("hash", "")
            if not b or ".." in b or b.startswith("refs/"):
                return httpx.Response(400, json={"error": {"message": "invalid branch name"}})
            if b in self.refs:
                return httpx.Response(400, json={"error": {"message": f"{b} already exists"}})
            if h not in self.commits:
                return httpx.Response(404, json={"error": {"message": "commit not found"}})
            self.refs[b] = h
            return httpx.Response(201, json={"type": "branch", "name": b, "target": {"hash": h}})
        mm = re.match(r"^/src/([^/]+)/(.*)$", rest)
        if mm and m == "GET":
            return self._src(mm.group(1), mm.group(2), q, url)
        if rest == "/src" and m == "POST":
            return self._commit(body or {})
        mm = re.match(r"^/commits/([0-9a-f]+)$", rest)
        if mm and m == "GET":
            a, ex = mm.group(1), q.get("exclude", "")
            if a not in self.commits or (ex and ex not in self.commits):
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            gone = self._ancestors(ex) if ex else set()
            vals = [
                {"hash": c, "parents": [{"hash": x} for x in self.commits[c]["parents"]]}
                for c in sorted(self._ancestors(a) - gone)
            ]
            if not ex and self.commits[a]["parents"] is not None:  # history: newest (a) first
                vals.sort(key=lambda v: v["hash"] != a)
            return httpx.Response(200, json={"values": vals[: int(q.get("pagelen", 30))]})
        if rest == "/pullrequests" and m == "GET":
            return self._list_prs(q, url)
        if rest == "/pullrequests" and m == "POST" and self.pr_fail:
            return httpx.Response(500, json={"error": {"message": "Server Error"}})
        if rest == "/pullrequests" and m == "POST":
            h = body["source"]["branch"]["name"]
            b = body["destination"]["branch"]["name"]
            if h not in self.refs or b not in self.refs:
                return httpx.Response(400, json={"error": {"message": "branch does not exist"}})
            if any(p["state"] == "OPEN" and p["head"] == h and p["base"] == b for p in self.prs):
                return httpx.Response(
                    400,
                    json={"error": {"message": "There is already an open pull request"}},
                )
            if self.refs[h] in self._ancestors(self.refs[b]):
                return httpx.Response(400, json={"error": {"message": "There are no changes"}})
            pr = {
                "number": len(self.prs) + 1,
                "head": h,
                "base": b,
                "state": "OPEN",
                "title": body["title"],
                "body": body.get("description", ""),
            }
            self.prs.append(pr)
            return httpx.Response(201, json=self._pr_json(pr))
        mm = re.match(r"^/pullrequests/(\d+)(/decline)?$", rest)
        if mm:
            pr = next((p for p in self.prs if p["number"] == int(mm.group(1))), None)
            if pr is None:
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            if mm.group(2) and m == "POST":
                if pr["state"] == "OPEN":
                    pr["state"] = "DECLINED"
                return httpx.Response(200, json=self._pr_json(pr))
            if m == "PUT":
                if pr["state"] != "OPEN":
                    return httpx.Response(400, json={"error": {"message": "not open"}})
                if self.strict_put and "title" not in body:
                    return httpx.Response(
                        400, json={"error": {"message": "title: This field is required."}}
                    )
                if "title" in body:
                    pr["title"] = body["title"]
                if "description" in body:
                    pr["body"] = body["description"]
            return httpx.Response(200, json=self._pr_json(pr))
        return httpx.Response(404, json={"error": {"message": f"unexpected {m} {rest}"}})

    # ------------------------------------------------------------------ src
    def _src(self, rev, p, q, url) -> httpx.Response:
        if not re.fullmatch(r"[0-9a-f]{40}", rev) or rev not in self.commits:
            return httpx.Response(404, json={"error": {"message": "commit not found"}})
        flat = self.trees[self.commits[rev]["tree"]]
        base = f"https://{self.HOST}/2.0/repositories/{REPO}/src/{rev}"
        p = p.rstrip("/") if p.endswith("/") else p
        is_dir_url = unquote(url.path).endswith("/")
        dirs = {x.rsplit("/", 1)[0] for x in flat if "/" in x}
        for d in list(dirs):
            while "/" in d:
                d = d.rsplit("/", 1)[0]
                dirs.add(d)
        if p in flat and not is_dir_url:
            data = self.blobs[flat[p]]
            mode = self.modes.get(p)
            if q.get("format") == "meta":
                attrs = [mode] if mode in ("link", "subrepository", "executable") else []
                return httpx.Response(
                    200,
                    json={
                        "type": "commit_file",
                        "path": p,
                        "size": len(data),
                        "attributes": attrs,
                    },
                )
            if p in self.lfs:
                return httpx.Response(301, headers={"location": "https://media.example/lfs"})
            return httpx.Response(200, content=data)
        if p in flat and is_dir_url:
            return httpx.Response(404, json={"error": {"message": "not a directory"}})
        if p and p not in dirs:
            return httpx.Response(404, json={"error": {"message": "path not found"}})
        if q.get("format") == "meta":
            return httpx.Response(200, json={"type": "commit_directory", "path": p})
        pre = p + "/" if p else ""
        entries = {}
        for x in sorted(flat):
            if not x.startswith(pre):
                continue
            rel = x[len(pre) :]
            head = rel.split("/")[0]
            if "/" in rel:
                entries[pre + head] = {"type": "commit_directory", "path": pre + head}
            else:
                mode = self.modes.get(x)
                entries[x] = {
                    "type": "commit_file",
                    "path": x,
                    "size": len(self.blobs[flat[x]]),
                    "attributes": (
                        [mode] if mode in ("link", "subrepository", "executable") else []
                    )
                    + (["lfs"] if x in self.lfs else []),
                }
        rows = list(entries.values())
        per = min(int(q.get("pagelen", 10)), self.max_pagelen)
        page = int(q.get("page", 1))
        self.dir_pages.append(page)
        out = {"pagelen": per, "page": page, "values": rows[(page - 1) * per : page * per]}
        if page * per < len(rows):
            out["next"] = f"{base}/{pre}?pagelen={per}&page={page + 1}"
        if self.evil_next:
            out["next"] = self.evil_next
        return httpx.Response(200, json=out)

    def _commit(self, body) -> httpx.Response:
        fields, files = body.get("fields", {}), body.get("files", {})
        b, parent = fields.get("branch"), fields.get("parents")
        if self.commit_error:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": f"rejected content for token {self.token} {self.token_echo}"
                    }
                },
            )
        if not b or not parent:
            return httpx.Response(400, json={"error": {"message": "branch and parents needed"}})
        if self.before_commit:
            hook, self.before_commit = self.before_commit, None
            hook(self, b)
        if b in self.refs and self.refs[b] != parent:
            return httpx.Response(409, json={"error": {"message": "parent is not the tip"}})
        if b not in self.refs and parent not in self.commits:
            return httpx.Response(400, json={"error": {"message": "unknown parent"}})
        if any(
            rule.get("kind") == "push" and fnmatch.fnmatchcase(b, rule.get("pattern", ""))
            for rule in self.restrictions
        ):
            return httpx.Response(
                403, json={"error": {"message": f"Branch {b} is restricted (push)"}}
            )
        flat = dict(self.trees[self.commits[parent]["tree"]])
        for fp, data in files.items():
            rel = fp.lstrip("/")
            if ".." in rel.split("/") or not rel:
                return httpx.Response(400, json={"error": {"message": "invalid path"}})
            if rel in ("message", "branch", "parents", "author", "files") and fp == rel:
                return httpx.Response(400, json={"error": {"message": "field clash"}})
            flat[rel] = self._blob(data)
            self.modes.pop(rel, None)  # an upload without x-attributes is a regular file
        csha = self._make_commit(flat, [parent], fields.get("message", ""))
        self.refs[b] = csha
        return httpx.Response(
            201,
            content=b"",
            headers={
                "location": getattr(self, "location_override", None)
                or f"https://{self.HOST}/2.0/repositories/{REPO}/commit/{csha}",
            }
            if not getattr(self, "no_location", False)
            else {},
        )

    # ---------------------------------------------------------- pull requests
    def _list_prs(self, q, url) -> httpx.Response:
        qs = q.get("q", "")
        mh = re.search(r'source\.branch\.name="([^"]*)"', qs)
        mb = re.search(r'destination\.branch\.name="([^"]*)"', qs)
        real = [
            self._pr_json(p)
            for p in self.prs
            if p["state"] == q.get("state", "OPEN")
            and (not mh or p["head"] == mh.group(1))
            and (not mb or p["base"] == mb.group(1))
        ]
        # a server that ignored the q filter would page through unrelated PRs first
        noise = [
            self._pr_json(
                {
                    "number": 1000 + i,
                    "head": f"noise-{i}",
                    "base": "main",
                    "state": "OPEN",
                    "title": "x",
                }
            )
            for i in range(self.noise_prs)
        ]
        rows = noise + real
        per, page = min(int(q.get("pagelen", 10)), self.max_pagelen), int(q.get("page", 1))
        self.pr_pages.append(page)
        out = {"pagelen": per, "page": page, "values": rows[(page - 1) * per : page * per]}
        if page * per < len(rows):
            nq = dict(q, page=page + 1)
            out["next"] = f"https://{self.HOST}/2.0/repositories/{REPO}/pullrequests?" + "&".join(
                f"{k}={quote(str(v))}" for k, v in nq.items()
            )
        return httpx.Response(200, json=out)

    def _pr_json(self, p: dict) -> dict:
        return {
            "id": p["number"],
            "state": p["state"],
            "title": p["title"],
            "description": p.get("body", ""),
            "links": {
                "html": {"href": f"https://bitbucket.test/{REPO}/pull-requests/{p['number']}"}
            },
            "source": {"branch": {"name": p["head"]}, "repository": {"full_name": REPO}},
            "destination": {"branch": {"name": p["base"]}},
        }


# ---------------------------------------------------------------------------
EN_JSON = {
    "app": {"title": "Welcome to the dashboard", "save": "Save your changes"},
    "items": {"one": "{{count}} item", "other": "{{count}} items"},
    "hello": "Hello there",
}
EN_PO = (
    'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n"Language: en\\n"\n'
    '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
    'msgid "Open settings"\nmsgstr "Open settings"\n\n'
    'msgid "%d file"\nmsgid_plural "%d files"\nmsgstr[0] "%d file"\nmsgstr[1] "%d files"\n'
)


def _json(obj) -> bytes:
    return json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"


def _repo_files():
    return {"locales/en-US.json": _json(EN_JSON), "po/en-US.po": EN_PO.encode("utf-8")}


def _conn(name, cfg_extra=None, secret=None):
    cfg = {
        "repo": REPO,
        "base_branch": "main",
        "source_paths": "locales/{locale}.json, po/{locale}.po",
        "source_locale": "en-US",
        "target_locales": "fr-FR, de-DE",
    }
    cfg.update(cfg_extra or {})
    return cstore.create("bitbucket", name, cfg, secret or {"token": TOKEN}, actor="admin")


def _run(conn, fake, **kw):
    kw.setdefault("actor", "auto")
    return cstore.bitbucket_translate(conn.id, transport=fake.transport(), **kw)


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _cfg(cid):
    with Session(engine) as s:
        return dict(s.get(Connection, cid).config or {})


def _status(cid):
    with Session(engine) as s:
        c = s.get(Connection, cid)
        return c.status, c.last_message


def _loaded(data: bytes, fmt: str, locale: str) -> dict:
    return {r["name"]: r for r in key_files.parse_keys(data, fmt, locale=locale)}


# ---------------------------------------------------------------------------
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            self.lines.append(str(record.msg))


def main() -> None:
    init_db()
    cap = _Capture()
    logging.getLogger().addHandler(cap)
    logging.getLogger().setLevel(logging.DEBUG)
    assert BitbucketConnector.capability.topology == "T3"

    # ---- (1) provider basics: test(), default branch, create_branch tolerates an existing one
    fake = FakeBitbucket(_repo_files())
    cx = BitbucketConnector(
        {"repo": REPO, "source_paths": "locales/{locale}.json"},
        {"token": TOKEN},
        transport=fake.transport(),
    )
    cx.connection_id = 1
    ok, msg = cx.test()
    assert ok and REPO in msg, msg
    assert "not readable" in msg, (
        "least-privilege token: restrictions unreadable is reported",
        msg,
    )
    prov = cx.provider()
    assert prov.default_branch() == "main"
    fake.refs["loc/1"] = fake.refs["main"]
    assert prov.create_branch("loc/1", fake.refs["main"]) == fake.refs["main"], (
        "existing branch -> its head"
    )
    bad = BitbucketConnector(
        {"repo": REPO, "source_paths": "locales/{locale}.json"},
        {"token": "wrong-token-123456"},
        transport=fake.transport(),
    )
    bad.connection_id = 1
    ok, msg = bad.test()
    assert not ok and "401" in msg and "wrong-token-123456" not in msg, msg
    adm = FakeBitbucket(_repo_files())
    adm.admin = True
    cadm = BitbucketConnector(
        {"repo": REPO, "source_paths": "locales/{locale}.json"},
        {"token": TOKEN},
        transport=adm.transport(),
    )
    cadm.connection_id = 1
    ok, msg = cadm.test()
    assert ok and "not readable" not in msg, msg
    # an Atlassian email switches the header to Basic email:token
    fe = FakeBitbucket(_repo_files(), email="dev@acme.test")
    ce = BitbucketConnector(
        {"repo": REPO, "source_paths": "locales/{locale}.json"},
        {"token": TOKEN, "email": "dev@acme.test"},
        transport=fe.transport(),
    )
    ce.connection_id = 1
    ok, msg = ce.test()
    assert ok, msg
    ro = FakeBitbucket(_repo_files(), push=False)
    cro = _conn("readonly", {"target_locales": "fr-FR"})
    ok, msg, _r = _run(cro, ro)
    assert not ok and "403" in msg and set(ro.refs) == {"main"} and not ro.prs, msg
    print(
        "  provider: test(), 401 scrubbed, Basic email form, restrictions-unreadable noted, "
        "read-only token fails loud, create_branch tolerates an existing branch (400)"
    )

    # ---- (2) first run: JSON nested + PO + plurals, ONE commit, ONE pull request
    fake = FakeBitbucket(_repo_files())
    c = _conn("first")
    main_before = fake.refs["main"]
    ok, msg, res = _run(c, fake)
    assert ok, msg
    assert res["created"] == 4 and res["updated"] == 0, res
    [x for x in fake.commits if x not in set(fake.commits) - set()]
    branch = f"loc/{c.id}"
    assert branch in fake.refs, fake.refs
    new_commits = fake._ancestors(fake.refs[branch]) - fake._ancestors(main_before)
    assert len(new_commits) == 1, ("exactly ONE new commit", len(new_commits))
    assert fake.refs["main"] == main_before, "base branch untouched"
    assert len(fake.prs) == 1 and fake.prs[0]["head"] == branch and fake.prs[0]["base"] == "main", (
        fake.prs
    )
    files = fake.files_at(branch)
    for p in ("locales/fr-FR.json", "locales/de-DE.json", "po/fr-FR.po", "po/de-DE.po"):
        assert p in files, (p, sorted(files))
    fr = json.loads(files["locales/fr-FR.json"])
    assert set(fr) == {"app", "items", "hello"} and set(fr["app"]) == {"title", "save"}, fr
    assert fr["hello"] != "Hello there" and fr["app"]["title"] != "Welcome to the dashboard", fr
    assert set(fr["items"]) >= {"one", "other"}, ("French plural categories", fr["items"])
    frpo = _loaded(files["po/fr-FR.po"], "po", "fr-FR")
    assert "Open settings" in frpo and frpo["Open settings"]["value"] != "Open settings", list(frpo)
    plural_key = next(r for r in frpo.values() if r.get("is_plural"))
    assert len(plural_key["plural_values"]) >= 2, plural_key
    assert files["locales/en-US.json"] == _json(EN_JSON), "source file is not rewritten"
    print(
        "  loop: nested JSON + PO + plurals translated, 4 files in ONE commit, ONE PR, base untouched"
    )

    # ---- (3) C/F: second run over unchanged source is a no-op, hash recorded only now
    hashes = _cfg(c.id).get("bitbucket_hashes") or {}
    assert len(hashes) == 4, hashes
    n_writes = len(fake.writes())
    ok, msg, res2 = _run(c, fake)
    assert ok and res2["skipped"] == 4 and not res2["commit"], (msg, res2)
    assert len(fake.writes()) == n_writes, "unchanged source: no writes at all"
    assert len(fake.prs) == 1
    print("  C: unchanged source -> 4 skipped, no commit, no PR change (steady state)")

    # ---- (4) source change: second commit on the SAME branch, SAME pull request (updated)
    head1 = fake.refs[branch]
    en2 = dict(EN_JSON, hello="Hello again, friend")
    fake.commit_to("main", {"locales/en-US.json": _json(en2)}, "edit source")
    main2 = fake.refs["main"]
    ok, msg, res3 = _run(c, fake)
    assert ok and res3["commit"], (msg, res3)
    assert fake.refs[branch] != head1 and head1 in fake._ancestors(fake.refs[branch])
    assert len(fake.prs) == 1 and fake.prs[0]["number"] == 1, (
        "one PR per connection, updated in place"
    )
    assert "fr-FR" in fake.prs[0]["title"]
    assert fake.refs["main"] == main2
    fr2 = json.loads(fake.files_at(branch)["locales/fr-FR.json"])
    assert fr2["hello"] != fr["hello"] and fr2["app"] == fr["app"], (fr["hello"], fr2["hello"])
    assert res3["created"] == 0 and res3["updated"] == 2, (
        "only the changed source's 2 files",
        res3,
    )
    print(
        "  loop: a source edit -> a 2nd commit on the same branch, the same PR updated, 2 files rewritten"
    )

    # ---- (5) merged PR: Bitbucket cannot move a branch, so a NEW branch + a NEW pull request
    fake.merge_pr(1)
    old_branch_head = fake.refs[branch]
    en3 = dict(EN_JSON, hello="Hello a third time")
    fake.commit_to("main", {"locales/en-US.json": _json(en3)}, "edit source again")
    ok, msg, _res4 = _run(c, fake)
    assert ok, msg
    assert len(fake.prs) == 2 and fake.prs[1]["state"] == "OPEN", fake.prs
    assert fake.prs[1]["head"] == f"{branch}-2" and fake.refs[branch] == old_branch_head, (
        "the merged branch is left alone; the next generation carries the work"
    )
    assert not [w for w in fake.writes() if w[1].endswith(f"/refs/branches/{branch}")], (
        "no call ever tries to move an existing branch"
    )
    print("  merged PR -> fresh branch loc/N-2 from the new base (no ref-update exists), new PR")

    # ---- (6) H: the base branch is never written, on the wire; one host only
    for m, p, b in fake.writes():
        assert not p.endswith("/branches/main"), (m, p)
        if p.endswith("/refs/branches"):
            assert b["name"] != "main", b
        if p.endswith("/src"):
            assert b["fields"]["branch"] != "main" and b["fields"]["parents"], (m, p)
    assert fake.hosts == {"api.bitbucket.org"}, fake.hosts
    print("  H: no wire request ever targeted main; every commit names branch + parents; one host")

    _more(cap)
    _review(cap)
    print("verify_bitbucket OK")


def _patch_pretranslate(hold_sources=None, blank=False):
    """Flag machine output NEEDS_REVIEW (a held-back unit): all of it, or only the segments
    whose source text is in `hold_sources`; `blank` also empties it (a quarantined refusal).
    Returns the restore callable."""
    orig = translate.pretranslate_document

    def patched(s, doc_id, loc, *a, **kw):
        out = orig(s, doc_id, loc, *a, **kw)
        for sg in s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == loc)
        ).all():
            if sg.status in (SegStatus.APPROVED, SegStatus.EDITED):
                continue
            if hold_sources is None or sg.source_text in hold_sources:
                sg.status = SegStatus.NEEDS_REVIEW
                if blank:
                    sg.target_text = ""
                s.add(sg)
        s.commit()
        return out

    translate.pretranslate_document = patched
    return lambda: setattr(translate, "pretranslate_document", orig)


def _set_row(source_text, locale, status=None, target=None):
    """Edit the key-project value row whose source text is `source_text` (a reviewer)."""
    with Session(engine) as s:
        rows = s.exec(
            select(Segment).where(
                Segment.source_text == source_text, Segment.target_locale == locale
            )
        ).all()
        assert rows, (source_text, locale)
        for sg in rows:
            if status is not None:
                sg.status = status
            if target is not None:
                sg.target_text = target
            s.add(sg)
        s.commit()


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


def _more(cap) -> None:
    from app.services import settings
    from app.services.connectors import egress_guard
    from app.services.connectors.store_common import dec_secret

    # ---- (7) B: a held-back key is never written as a translation, no hash until approved
    fake = FakeBitbucket(_repo_files())
    c = _conn("hold", {"target_locales": "fr-FR, de-DE"})
    restore = _patch_pretranslate({"Hello there"})
    try:
        ok, msg, res = _run(c, fake)
        assert ok, msg
        assert res["held_back"] == 2, res
        files = fake.files_at(f"loc/{c.id}")
        for loc in ("fr-FR", "de-DE"):
            d = json.loads(files[f"locales/{loc}.json"])
            assert "hello" not in d and "app" in d and "items" in d, (loc, d)
        hashes = _cfg(c.id).get("bitbucket_hashes") or {}
        assert "locales/en-US.json:fr-FR" not in hashes and "po/en-US.po:fr-FR" in hashes, hashes
        assert "held back" in msg, msg
        w = len(fake.writes())
        ok, msg, res = _run(c, fake)
        assert ok and not res["commit"] and len(fake.writes()) == w, ("held pair, no churn", res)
    finally:
        restore()
    _set_row("Hello there", "fr-FR", status=SegStatus.APPROVED)  # a reviewer approves fr only
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"], (msg, res)
    d = json.loads(fake.files_at(f"loc/{c.id}")["locales/fr-FR.json"])
    assert d["hello"], d
    assert "hello" not in json.loads(fake.files_at(f"loc/{c.id}")["locales/de-DE.json"])
    hashes = _cfg(c.id).get("bitbucket_hashes") or {}
    assert "locales/en-US.json:fr-FR" in hashes, hashes
    assert "locales/en-US.json:de-DE" not in hashes, hashes
    _set_row("Hello there", "de-DE", status=SegStatus.APPROVED)
    ok, msg, res = _run(c, fake)
    assert ok and "hello" in json.loads(fake.files_at(f"loc/{c.id}")["locales/de-DE.json"]), msg
    assert "locales/en-US.json:de-DE" in (_cfg(c.id).get("bitbucket_hashes") or {})
    print("  B: a held key is omitted (never a fake translation), retried, hashed once approved")

    # ---- (8) B: the value that already ships in the repository survives a held run
    existing = {
        "app": {"title": "Bienvenue", "save": "Enregistrer"},
        "items": {
            "one": "{{count}} article",
            "many": "{{count}} d'articles",
            "other": "{{count}} articles",
        },
        "hello": "Salut tout le monde",
    }
    fake = FakeBitbucket({**_repo_files(), "locales/fr-FR.json": _json(existing)})
    c = _conn("keep", {"target_locales": "fr-FR"})
    ok, msg, res = _run(c, fake)
    assert ok, msg
    br = f"loc/{c.id}"
    assert json.loads(fake.files_at(br)["locales/fr-FR.json"])["hello"] == "Salut tout le monde", (
        "an existing repository translation is reused, not replaced by machine output"
    )
    assert "po/fr-FR.po" in fake.files_at(br)
    restore = _patch_pretranslate({"Hello once more"})
    try:
        fake.commit_to(
            "main", {"locales/en-US.json": _json(dict(EN_JSON, hello="Hello once more"))}
        )
        ok, msg, res = _run(c, fake)
        assert ok, msg
        assert res["held_back"] == 1 and res["held"][0]["kept"] == 1, res
        assert json.loads(fake.files_at(br)["locales/fr-FR.json"])["hello"] == "Salut tout le monde"
    finally:
        restore()
    # a human-approved value is not re-translated by the connector when its source changes
    _set_row(
        "Welcome to the dashboard", "fr-FR", status=SegStatus.APPROVED, target="Accueil humain"
    )
    changed = dict(
        EN_JSON, hello="Hello once more", app={"title": "Welcome back", "save": "Save your changes"}
    )
    fake.commit_to("main", {"locales/en-US.json": _json(changed)})
    ok, msg, res = _run(c, fake)
    assert ok and res["held_back"] >= 1, (msg, res)
    d = json.loads(fake.files_at(br)["locales/fr-FR.json"])
    assert d["app"]["title"] == "Bienvenue", (
        "stale approved value held, repo value kept",
        d["app"],
    )
    print(
        "  B: an existing repo translation survives a held run; approved work is never re-translated"
    )

    # ---- (9) F: a run where nothing could be translated writes nothing and records no hash
    fake = FakeBitbucket(_repo_files())
    c = _conn("allheld", {"target_locales": "fr-FR"})
    restore = _patch_pretranslate(None, blank=True)
    try:
        ok, msg, res = _run(c, fake)
    finally:
        restore()
    assert not ok and not fake.writes() and not _cfg(c.id).get("bitbucket_hashes"), (msg, res)
    assert f"loc/{c.id}" not in fake.refs, "no branch for an empty run"
    assert _status(c.id)[0] == "error"
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"], (msg, res)
    print("  F: nothing translated -> no branch, no commit, no hash; the healthy re-run completes")

    # ---- (10) A: hostile article ids are refused before any request
    fake = FakeBitbucket(_repo_files())
    c = _conn("scope", {"target_locales": "fr-FR"})
    hostile = [
        "../etc/passwd",
        "locales/../../etc/passwd",
        "locales/%2e%2e%2fsecret.json",
        "%2e%2e%2f",
        "locales/en-US.json?ref=evil",
        "locales/en-US.json#frag",
        "locales\\en-US.json",
        "/locales/en-US.json",
        "locales/en-US.json\x00",
        "locales/en-US.json\r\nX-Evil: 1",
        "locales/en-US.jso",
        "locales/en-US.json2",
        "locales/en-US.json/",
        ".git/config",
        "locales/‮en-US.json",
        "other/en-US.json",
        "LOCALES/EN-US.JSON",
        "",
        "locales//en-US.json",
        "locales/./en-US.json",
        123,
        None,
        {"a": 1},
        "locales/en-US.json ",
        " locales/en-US.json",
    ]
    n0 = len(fake.log)
    for bad in hostile:
        ok, msg, res = _run(c, fake, article_ids=[bad])
        assert not ok and len(fake.log) == n0, ("hostile id reached the wire", bad, msg)
    ok, msg, res = _run(c, fake, article_ids=["locales/en-US.json", "../x"])
    assert not ok and len(fake.log) == n0, "one bad id refuses the whole run"
    ok, msg, res = _run(c, fake, article_ids=["locales/en-US.json"])
    assert ok and res["created"] == 1, ("only the named source file is processed", res)
    print("  A: %d hostile article ids refused with zero wire traffic" % len(hostile))

    # ---- (11) A: hostile operator configuration is refused before any request
    bad_cfgs = [
        {"repo": "acme/../../x"},
        {"repo": "acme"},
        {"repo": "acme/site/extra"},
        {"repo": "../site"},
        {"repo": "acme/site;rm"},
        {"repo": "acme/site.git"},
        {"base_branch": "main/../x"},
        {"base_branch": "-evil"},
        {"base_branch": "main branch"},
        {"branch_pattern": "main"},
        {"branch_pattern": "loc/../main"},
        {"branch_pattern": "refs/heads/x"},
        {"branch_pattern": "loc/{connection}.lock"},
        {"branch_pattern": "loc/x y"},
        {"branch_pattern": "loc/%2e%2e"},
        {"source_paths": "../locales/{locale}.json"},
        {"source_paths": "locales/{locale}.txt"},
        # .yaml is a key format since Batch 9 (keys bridge); a format with no key view is not
        {"source_paths": "locales/{locale}.docx"},
        {"source_paths": "locales/en-US.json"},
        {"source_paths": "/abs/{locale}.json"},
        {"source_paths": "locales/{locale}.json?x"},
        {"source_paths": "a{b}/{locale}.json"},
        {"source_paths": "locales/{locale}.pot"},
        {"source_paths": ".git/{locale}.json"},
        {"source_paths": "locales/{locale}.json\x00"},
        {"locale_style": "weird"},
        {"plural_style": "weird"},
        {"workflow_id": "abc"},
        {"source_locale": "en-US/../x"},
        {"source_locale": "not a locale"},
        {"locale_style": "language", "target_locales": "fr-FR, fr-CA"},
        {"locale_style": "language", "target_locales": "en-GB"},
        {"branch_pattern": "loc-static"},
        {"branch_pattern": "HEAD"},
        {"branch_pattern": "loc/{connection}/.git::$INDEX_ALLOCATION"},
        {"source_paths": "locales/.git::$INDEX_ALLOCATION/{locale}.json"},
        {"source_paths": "GIT~1/{locale}.json"},
    ]
    n0 = len(fake.log)
    for extra in bad_cfgs:
        cx = _conn("bad", dict({"target_locales": "fr-FR"}, **extra))
        ok, msg, res = _run(cx, fake)
        assert not ok and len(fake.log) == n0, ("hostile config reached the wire", extra, msg)
    # there is no URL field: a base_url smuggled into the config goes nowhere
    fake_h = FakeBitbucket(_repo_files())
    ch = _conn("nohost", {"target_locales": "fr-FR", "base_url": "http://169.254.169.254"})
    ok, msg, res = _run(ch, fake_h)
    assert ok and fake_h.hosts == {"api.bitbucket.org"}, (msg, fake_h.hosts)
    ok, msg, res = _run(c, fake, target_locales="../../etc, fr-FR/../x")
    assert not [w for w in fake.writes() if ".." in w[1]], "locale override cannot traverse"
    print(
        "  A: %d hostile repo/branch/path/locale/host settings refused, zero wire traffic"
        % len(bad_cfgs)
    )

    # ---- (12) the base branch is never written, even when asked to (branch == default)
    fake = FakeBitbucket(_repo_files())
    cx = _conn("tobase", {"branch_pattern": "main", "target_locales": "fr-FR"})
    ok, msg, _r = _run(cx, fake)
    assert not ok and not fake.writes() and "base" in msg.lower(), msg
    fake2 = FakeBitbucket(_repo_files())
    cx = _conn("todefault", {"branch_pattern": "release/{connection}", "target_locales": "fr-FR"})
    fake2.default_branch = f"release/{cx.id}"
    fake2.refs[fake2.default_branch] = fake2.refs["main"]
    ok, msg, _r = _run(cx, fake2)
    assert not ok and not fake2.writes() and "default" in msg.lower(), msg
    print("  H: a working branch equal to the base or the repository default branch is refused")

    # ---- (13) G: a 409 on a stale parent is loud, never an overwrite
    fake = FakeBitbucket(_repo_files())
    c = _conn("race", {"target_locales": "fr-FR"})

    def stray(f, b):
        f.commit_to(b, {"stray.txt": b"someone else pushed"}, "stray")

    fake.before_commit = stray
    ok, msg, res = _run(c, fake)
    assert not ok and "moved" in msg, msg
    assert fake.files_at(f"loc/{c.id}").get("stray.txt") == b"someone else pushed"
    assert "locales/fr-FR.json" not in fake.files_at(f"loc/{c.id}")
    assert not _cfg(c.id).get("bitbucket_hashes"), "a failed push records no hash"
    ok, msg, res = _run(c, fake)
    # Gate 2026-10-10: the stray commit is someone else's, so the core never builds on it;
    # with no pull request open it starts a fresh branch from the base and leaves loc/N alone.
    stray_head = fake.refs[f"loc/{c.id}"]
    assert ok and "locales/fr-FR.json" in fake.files_at(f"loc/{c.id}-2"), (msg, sorted(fake.refs))
    assert fake.refs[f"loc/{c.id}"] == stray_head, "the stray branch is never written"
    assert "stray.txt" not in fake.files_at(f"loc/{c.id}-2")
    print(
        "  G: stale parents 409 -> failed run, nothing overwritten, no hash; next run uses a new branch"
    )

    # ---- (14) pagination: a paginated src listing, PR lists past page 1, a foreign next link
    many = {f"locales/lang{i:02d}.json": b"{}" for i in range(23)}
    fake = FakeBitbucket({**_repo_files(), **many})
    fake.max_pagelen = 5
    cx = BitbucketConnector(
        {"repo": REPO, "source_paths": "locales/{locale}.json"},
        {"token": TOKEN},
        transport=fake.transport(),
    )
    cx.connection_id = 1
    prov = cx.provider()
    got = {f.path for f in prov.list_files(fake.refs["main"], "locales") if f.kind == "file"}
    want = {p for p in fake.files_at("main") if p.startswith("locales/")}
    assert got == want, (len(got), len(want))
    assert max(fake.dir_pages) >= 5, ("listing followed next links", fake.dir_pages)
    c = _conn("trunc", {"target_locales": "fr-FR"})
    ok, msg, res = _run(c, fake)
    assert ok and res["created"] == 2, (msg, res)
    fake = FakeBitbucket(_repo_files())
    fake.noise_prs = 150
    fake.max_pagelen = 50
    c = _conn("noise", {"target_locales": "fr-FR"})
    ok, _m, _r = _run(c, fake)
    assert ok and len(fake.prs) == 1
    fake.commit_to("main", {"locales/en-US.json": _json(dict(EN_JSON, hello="Another"))})
    ok, _m, _r = _run(c, fake)
    assert ok and len(fake.prs) == 1 and 3 in fake.pr_pages, ("PR found on page 3", fake.pr_pages)
    # a next link that leaves the repository is never followed (the token rides on it)
    fake = FakeBitbucket(_repo_files())
    fake.evil_next = "https://evil.example/2.0/steal"
    fake.max_pagelen = 5
    cx = BitbucketConnector(
        {"repo": REPO, "source_paths": "locales/{locale}.json"},
        {"token": TOKEN},
        transport=fake.transport(),
    )
    cx.connection_id = 1
    try:
        cx.provider().list_files(fake.refs["main"], "locales")
    except Exception as exc:
        assert "outside this repository" in str(exc), exc
    else:
        _fail("a foreign next link was followed")
    assert fake.hosts == {"api.bitbucket.org"}, fake.hosts
    print("  pagination: src listing + PR list follow next; a foreign next link is refused")

    # ---- (15) H: WriteBudget ceiling (one unit per file); the rest stays pending
    fake = FakeBitbucket(_repo_files())
    c = _conn("budget", {"target_locales": "fr-FR, de-DE"})
    saved = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        ok, msg, res = _run(c, fake)
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    assert res["ceiling_hit"] and res["created"] == 1 and len(res["failures"]) == 3, res
    assert len(_cfg(c.id).get("bitbucket_hashes") or {}) == 1
    ok, msg, res = _run(c, fake)
    assert ok and res["created"] == 3 and not res["ceiling_hit"], (msg, res)
    print("  H: write ceiling stops after 1 file; the next run completes the other 3")

    # ---- (16) H: mock MT + real egress is refused (no external writes)
    fake = FakeBitbucket(_repo_files())
    c = _conn("egress", {"target_locales": "fr-FR"})
    real = egress_guard.egress_is_real
    egress_guard.egress_is_real = lambda t: True
    try:
        ok, msg, _r = _run(c, fake)
    finally:
        egress_guard.egress_is_real = real
    assert not ok and not fake.log and "refusing" in msg.lower(), msg
    print("  H: egress guard - offline mock MT is never written to a real repository")

    # ---- (17) H: the token is never logged, stored in the clear, or echoed
    fake = FakeBitbucket(_repo_files())
    fake.commit_error = True
    c = _conn("leak", {"target_locales": "fr-FR"})
    ok, msg, res = _run(c, fake)
    assert not ok and TOKEN not in msg and TOKEN not in json.dumps(res), msg
    fake.commit_error = False
    ok, msg, res = _run(c, fake)
    assert ok
    with Session(engine) as s:
        blob = json.dumps(
            [
                {"c": dict(x.config or {}), "m": x.last_message}
                for x in s.exec(select(Connection)).all()
            ]
            + [x.message for x in s.exec(select(SyncLog)).all()]
        )
    assert TOKEN not in blob, "token in a stored config, status or sync log"
    assert not any(TOKEN in line for line in cap.lines), "token in a log line"
    assert dec_secret(cstore.get(c.id).secret_enc)["token"] == TOKEN
    for method, path, body in fake.log:
        assert TOKEN not in path and TOKEN not in repr(body), (method, path)
    print(
        "  H: token absent from messages, results, logs, stored config, sync log, URLs and bodies"
    )

    # ---- (18) credential pin: a different repository clears the stored token and email
    base = {"repo": REPO, "source_paths": "a/{locale}.json"}
    sec = {"token": TOKEN, "email": "dev@acme.test"}
    c = cstore.create("bitbucket", "pin", dict(base), dict(sec), actor="a")
    cstore.update(c.id, "pin", {"base_branch": "dev"}, None, actor="a")
    assert dec_secret(cstore.get(c.id).secret_enc).get("token") == TOKEN
    cstore.update(c.id, "pin", {"repo": "acme/other"}, None, actor="a")
    kept = dec_secret(cstore.get(c.id).secret_enc)
    assert not kept.get("token") and not kept.get("email"), "repo change clears the credential"
    from app.services.connectors.store_part2 import _FIXED_HOST_KINDS

    assert _FIXED_HOST_KINDS["bitbucket"] == frozenset({"api.bitbucket.org"})
    print("  H: repository change clears the stored token and email; same-repo edits keep it")

    # ---- (19) locale styles + removed keys + retry bypass + missing source
    fake = FakeBitbucket({"i18n/en.json": _json({"a": "One", "b": "Two"})})
    c = _conn(
        "style",
        {
            "source_paths": "i18n/{locale}.json",
            "locale_style": "language",
            "target_locales": "fr-FR, de-DE",
        },
    )
    ok, msg, res = _run(c, fake)
    assert ok and {"i18n/fr.json", "i18n/de.json"} <= set(fake.files_at(f"loc/{c.id}")), msg
    fake.commit_to("main", {"i18n/en.json": _json({"a": "One"})})
    ok, msg, res = _run(c, fake)
    fr = json.loads(fake.files_at(f"loc/{c.id}")["i18n/fr.json"])
    assert ok and "b" not in fr, "a key removed from the source leaves the target file"
    ok, msg, res = _run(c, fake, only_pairs={("i18n/en.json", "fr-fr")})
    assert ok and res["skipped"] == 0 and res["unchanged"] == 1, ("retry bypasses hash", res)
    cm = _conn("nosrc", {"source_paths": "nope/{locale}.json", "target_locales": "fr-FR"})
    ok, msg, res = _run(cm, fake)
    assert not ok and fake.log[-1][0] == "GET", (msg, res)
    assert any("not found" in f["error"] for f in res["failures"]), res
    print(
        "  locale_style=language; removed key leaves the file; retry bypasses hash; missing source"
    )

    # ---- (20) key projects off -> a clear refusal; on again -> works
    fake = FakeBitbucket(_repo_files())
    c = _conn("off", {"target_locales": "fr-FR"})
    settings.set_many({"key_projects_enabled": "0"}, actor="t")
    try:
        ok, msg, _r = _run(c, fake)
    finally:
        settings.set_many({"key_projects_enabled": "1"}, actor="t")
    assert not ok and "key project" in msg.lower() and not fake.writes(), msg
    print("  key projects off -> clean refusal, nothing written")

    # ---- (21) four-surface parity
    from fastapi.testclient import TestClient
    from starlette.routing import Route

    from app.main import app
    from app.services.connectors import REGISTRY
    from app.web._routes import walk_routes

    assert "bitbucket" in REGISTRY
    tc = TestClient(app)
    assert "Bitbucket" in tc.get("/connectors").text
    typ = tc.get("/connectors/bitbucket").text
    assert "Repository" in typ and "API token or repository access token" in typ, (
        "type form renders"
    )
    from app.cli.__main__ import build_parser

    assert "connector bitbucket-translate" in _leaf_paths(build_parser())
    assert "/api/connectors/{cid}/bitbucket/translate" in {
        r.path for r in walk_routes(app) if isinstance(r, Route)
    }
    print("  parity: REGISTRY + /connectors tile + type form + /api route + CLI leaf")


def _review(cap) -> None:
    """Independent-review additions: path/branch hardening, symlinks and submodules, protected
    branch, repository identity, token shape, an orphaned branch, the workflow translate path."""
    from app.services import keys as keys_svc
    from app.services.connectors import git_core
    from app.services.keys_hub import translatable_documents

    # ---- (22) path / branch / locale unit rules
    for bad in (
        ".git::$INDEX_ALLOCATION/x.json",
        "locales/git~1/x.json",
        "locales/GIT~2",
        "locales/a:b.json",
        "locales/‮en.json",
        "locales/​../x.json",
        "locales/.git/x.json",
        "locales/..\\x.json",
    ):
        try:
            git_core.validate_repo_path(bad)
        except git_core.GitRefused:
            continue
        _fail(f"path accepted: {bad!r}")
    for bad in ("HEAD", "head", "loc/​x", "refs/heads/x", "loc/x.lock", "a..b", "x/.hidden"):
        try:
            git_core.validate_branch(bad)
        except git_core.GitRefused:
            continue
        _fail(f"branch accepted: {bad!r}")
    for bad in ("fr\nx", "fr-FR/../x", "ｆｒ", "fr-FR\x00", "fr_", "..", "f"):
        try:
            git_core.resolve_pattern("locales/{locale}.json", bad, "bcp47")
        except git_core.GitRefused:
            continue
        _fail(f"locale accepted in a file name: {bad!r}")
    print("  A: encoded/ADS/8.3/invisible path, HEAD/lock branch and look-alike locale rules hold")

    # ---- (23) a symlink in the way is never replaced; a symlinked source is refused
    fake = FakeBitbucket(_repo_files())
    fake.commit_to("main", {"locales/fr-FR.json": b"../shared/fr.json"})
    fake.modes["locales/fr-FR.json"] = "link"
    c = _conn(
        "symlink", {"target_locales": "fr-FR, de-DE", "source_paths": "locales/{locale}.json"}
    )
    ok, msg, res = _run(c, fake)
    files = fake.files_at(f"loc/{c.id}")
    assert files["locales/fr-FR.json"] == b"../shared/fr.json", "the symlink was replaced"
    assert "locales/de-DE.json" in files, "the other locale still lands"
    assert any("symlink" in f["error"] for f in res["failures"]), res
    assert "locales/en-US.json:fr-FR" not in (_cfg(c.id).get("bitbucket_hashes") or {}), "no hash"
    fake = FakeBitbucket(_repo_files())
    fake.modes["locales/en-US.json"] = "link"
    c = _conn("srclink", {"target_locales": "fr-FR", "source_paths": "locales/{locale}.json"})
    ok, msg, res = _run(c, fake)
    assert not fake.writes() and any("symlink" in f["error"] for f in res["failures"]), (msg, res)
    print(
        "  A: a symlink at a target path is not replaced; a symlinked source is refused, no writes"
    )

    # ---- (24) a protected working branch: refused up front when restrictions are readable,
    # and by the host (403, nothing written) when the token cannot read them
    fake = FakeBitbucket(_repo_files())
    fake.admin = True
    fake.restrictions = [{"kind": "push", "branch_match_kind": "glob", "pattern": "loc/*"}]
    c = _conn("prot", {"target_locales": "fr-FR"})
    ok, msg, _r = _run(c, fake)
    assert not ok and "protected" in msg and not fake.writes(), msg
    fake = FakeBitbucket(_repo_files())
    fake.restrictions = [{"kind": "push", "branch_match_kind": "glob", "pattern": "loc/*"}]
    c = _conn("prot2", {"target_locales": "fr-FR"})
    ok, msg, _r = _run(c, fake)
    assert not ok and "403" in msg and not fake.prs, msg
    assert fake.files_at("main") == _repo_files() and all(
        "locales/fr-FR.json" not in fake.files_at(r) for r in fake.refs
    ), "restricted branch: no translation landed anywhere"
    print("  H: protected branch refused (restrictions readable) or by the host (403), no commit")

    # ---- (25) repository identity pinned in the answer; archived repo refused
    fake = FakeBitbucket(_repo_files())
    fake.full_name = "evil/other"
    c = _conn("ident", {"target_locales": "fr-FR"})
    ok, msg, _r = _run(c, fake)
    assert not ok and "evil/other" in msg and not fake.writes(), msg
    print("  H: an answer for a different repository is refused")

    # ---- (26) a malformed token is refused without being echoed or sent
    fake = FakeBitbucket(_repo_files())
    weird = "ATATT3xSECRETPART one\ntwo_0123456789"  # pregate: allow-secret offline fixture
    c = _conn("badtok", {"target_locales": "fr-FR"}, {"token": weird})
    ok, msg, res = _run(c, fake)
    assert not ok and not fake.log and "SECRETPART" not in msg, msg
    assert not any("SECRETPART" in line for line in cap.lines), "token fragment in a log"
    print("  H: a token with spaces or line breaks is refused, never echoed, never sent")

    # ---- (27) commit landed but the PR open failed: the next run opens the PR, no new commit
    fake = FakeBitbucket(_repo_files())
    c = _conn("orphan", {"target_locales": "fr-FR"})
    fake.pr_fail = True
    ok, msg, res = _run(c, fake)
    br = f"loc/{c.id}"
    assert not ok and br in fake.refs and not fake.prs, (msg, fake.prs)
    assert not _cfg(c.id).get("bitbucket_hashes"), "no hash when the PR was not opened"
    fake.pr_fail = False
    head = fake.refs[br]
    ok, msg, res = _run(c, fake)
    assert ok and len(fake.prs) == 1 and fake.refs[br] == head, (msg, fake.prs)
    assert res["pull_request"]["number"] == 1 and not res["commit"], res
    assert len(_cfg(c.id).get("bitbucket_hashes") or {}) == 2, _cfg(c.id)
    n = len(fake.writes())
    ok, msg, res = _run(c, fake)
    assert ok and res["skipped"] == 2 and len(fake.writes()) == n, "steady state afterwards"
    print("  E: an orphaned working branch gets its pull request on the next run (no new commit)")

    # ---- (28) workflow_id: the workflow path replaces the direct pretranslate and reports
    fake = FakeBitbucket(_repo_files())
    c = _conn("wf", {"target_locales": "fr-FR", "workflow_id": "42"})
    real_launch = keys_svc.launch_translation
    real_pre = translate.pretranslate_document
    calls: list = []

    def spy(sess, proj, workflow_id, locale, *, actor, wait=False, **kw):
        calls.append((workflow_id, locale, wait))
        for doc in translatable_documents(sess, proj):  # stands in for the workflow's engine
            real_pre(sess, doc.id, locale)
        return {"drain_incomplete": [{"run_id": 77}]}

    def no_direct(*a, **k):
        raise AssertionError("the direct pretranslate path ran although workflow_id is set")

    keys_svc.launch_translation = spy
    translate.pretranslate_document = no_direct
    try:
        ok, msg, res = _run(c, fake)
    finally:
        keys_svc.launch_translation = real_launch
        translate.pretranslate_document = real_pre
    assert calls and all(x == (42, "fr-FR", True) for x in calls), calls
    assert ok and res["commit"] and "locales/fr-FR.json" in fake.files_at(f"loc/{c.id}"), (msg, res)
    assert any("77" in e and "did not complete" in e for e in res["errors"]), res["errors"]
    assert "warning" in msg, msg
    fr = json.loads(fake.files_at(f"loc/{c.id}")["locales/fr-FR.json"])
    assert fr["hello"] != "Hello there", "the workflow's output reaches the file"
    print("  workflow_id: launch_translation(wait=True) drives the run; incomplete runs reported")
    _gate_branches()
    _bb_specific()


def _gate_branches() -> None:
    """Review gate 2026-10-10: branch ownership, base moved under an open PR, reviewer edits."""
    from app.services.connectors.git_core import BODY_BEGIN, BODY_END

    def edit_source(fake, hello):
        fake.commit_to("main", {"locales/en-US.json": _json(dict(EN_JSON, hello=hello))})

    # ---- (29) a branch with the working-branch name that this connection did not create
    fake = FakeBitbucket(_repo_files())
    c = _conn("foreign", {"target_locales": "fr-FR"})
    br = f"loc/{c.id}"
    fake.refs[br] = fake.refs["main"]
    human = fake.commit_to(br, {"notes.txt": b"human work"}, "human")
    ok, msg, _r = _run(c, fake)
    assert not ok and "not created by this connection" in msg, msg
    assert not fake.writes() and not fake.prs and fake.refs[br] == human, fake.writes()
    # the ownership record is server-owned: a forged one through the config API is dropped
    cstore.update(
        c.id,
        c.name,
        {"bitbucket_branch_state": {"branch": br, "head": human, "base": human, "gen": 1}},
        None,
    )
    assert "bitbucket_branch_state" not in _cfg(c.id), _cfg(c.id)
    ok, msg, _r = _run(c, fake)
    assert not ok and "not created by this connection" in msg and not fake.writes(), msg
    print(
        "  H: a same-named branch this connection did not create is refused; forged record dropped"
    )

    # ---- (30) reviewer edits to the PR title/body survive; base moved without touching
    # our files keeps the same branch and PR
    fake = FakeBitbucket(_repo_files())
    c = _conn("review", {"target_locales": "fr-FR"})
    br = f"loc/{c.id}"
    ok, msg, res = _run(c, fake)
    assert ok and len(fake.prs) == 1, (msg, fake.prs)
    pr1 = fake.prs[0]
    assert pr1["body"].count(BODY_BEGIN) == 1 and pr1["body"].count(BODY_END) == 1, pr1["body"]
    pr1["title"] = "Reviewer title"
    pr1["body"] = "Reviewer notes on top.\n\n" + pr1["body"] + "\n\nSigned off: Ana"
    edit_source(fake, "Hello again, reviewer")
    head1 = fake.refs[br]
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"] and len(fake.prs) == 1, (msg, fake.prs)
    assert head1 in fake._ancestors(fake.refs[br]), "base moved, our files untouched: same branch"
    assert pr1["title"] == "Reviewer title", pr1["title"]
    assert pr1["body"].startswith("Reviewer notes on top.") and pr1["body"].endswith(
        "Signed off: Ana"
    ), pr1["body"]
    assert pr1["body"].count(BODY_BEGIN) == 1 and "locales/fr-FR.json" in pr1["body"], pr1["body"]
    assert not any(w[2] and "title" in w[2] for w in fake.writes() if "/pullrequests/" in w[1]), (
        "an update never sends a title"
    )
    print("  PR: a reviewer's title and text outside the marked block survive an update")

    # ---- (31) base changed one of our target files under the open PR: a new branch from
    # the base carries everything, a new PR names the old one, the old one is closed
    fake.commit_to("main", {"locales/fr-FR.json": _json({"hello": "Bonjour (humain)"})})
    edit_source(fake, "Hello, base moved")
    old_head = fake.refs[br]
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"], (msg, res)
    assert fake.refs[br] == old_head, "the conflicting branch is never rewritten"
    br2 = f"loc/{c.id}-2"
    assert br2 in fake.refs and fake.refs["main"] in fake._ancestors(fake.refs[br2]), fake.refs
    assert {"locales/fr-FR.json", "po/fr-FR.po"} <= set(fake.files_at(br2)), sorted(
        fake.files_at(br2)
    )
    assert len(fake.prs) == 2 and pr1["state"] == "DECLINED", fake.prs
    pr2 = fake.prs[1]
    assert pr2["head"] == br2 and pr2["state"] == "OPEN" and "#1" in pr2["body"], pr2
    assert res["pull_request"]["number"] == 2 and any("#1" in n for n in res["notes"]), res
    print("  base moved over our files: new branch from base, new PR names #1, #1 declined")

    # ---- (32) someone pushes to our open PR branch: the run refuses to write on top
    reviewer = fake.commit_to(br2, {"locales/fr-FR.json": b'{"hello": "Salut (relu)"}\n'})
    edit_source(fake, "Hello, after review")
    n = len(fake.writes())
    ok, msg, _r = _run(c, fake)
    assert not ok and "#2" in msg and "someone else" in msg, msg
    assert len(fake.writes()) == n and fake.refs[br2] == reviewer, "nothing written on top"
    # once that PR is merged, the next run starts a fresh branch and leaves br2 alone
    fake.merge_pr(2)
    edit_source(fake, "Hello, after merge")
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"] and f"loc/{c.id}-3" in fake.refs, (msg, sorted(fake.refs))
    assert fake.refs[br2] == reviewer and fake.prs[-1]["head"] == f"loc/{c.id}-3", fake.prs
    print("  reviewer commits on an open PR are never buried; after the merge a fresh branch")

    # ---- (33) PR closed without merging (or squash-merged), base moved: a new branch from
    # the base, the old branch untouched, the closed PR not reopened
    fake = FakeBitbucket(_repo_files())
    c = _conn("squash", {"target_locales": "fr-FR"})
    br = f"loc/{c.id}"
    ok, msg, _r = _run(c, fake)
    assert ok and len(fake.prs) == 1, msg
    fake.prs[0]["state"] = "DECLINED"
    head = fake.refs[br]
    edit_source(fake, "Hello, squash")
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"] and fake.refs[br] == head, (msg, res)
    assert f"loc/{c.id}-2" in fake.refs and len(fake.prs) == 2, (sorted(fake.refs), fake.prs)
    assert fake.prs[0]["state"] == "DECLINED" and fake.prs[1]["state"] == "OPEN", fake.prs
    assert set(fake.files_at(f"loc/{c.id}-2")) >= {"locales/fr-FR.json"}
    print("  closed/squashed PR + base moved: new branch from base, old one untouched")


def _bb_specific() -> None:
    """Bitbucket-only behavior behind the provider interface."""
    from app.services.connectors import base as cbase
    from app.services.connectors import git_core
    from app.services.connectors.git_core import BODY_BEGIN

    def prov(fake, **secret):
        cx = BitbucketConnector(
            {"repo": REPO, "source_paths": "locales/{locale}.json"},
            {"token": TOKEN, **secret},
            transport=fake.transport(),
        )
        cx.connection_id = 1
        return cx.provider()

    # ---- (34) the commit is ONE multipart POST /src: branch + parents + message, every file
    # under a leading-slash field name (a file called "message" stays a file); a stale parent
    # is a 409 and nothing lands
    fake = FakeBitbucket(_repo_files())
    p = prov(fake)
    fake.refs["w"] = fake.refs["main"]
    tip = fake.refs["main"]
    sha = p.commit_files(
        "w", tip, "subject line", {"message": b"m", "branch": b"b", "a/b.json": b"{}"}
    )
    posts = [w for w in fake.writes() if w[1].endswith("/src")]
    assert len(posts) == 1, posts
    body = posts[0][2]
    assert body["fields"] == {"message": "subject line", "branch": "w", "parents": tip}, body[
        "fields"
    ]
    assert set(body["files"]) == {"/message", "/branch", "/a/b.json"}, set(body["files"])
    assert fake.files_at("w")["message"] == b"m" and fake.files_at("w")["a/b.json"] == b"{}"
    assert sha == fake.refs["w"] and fake.commits[sha]["parents"] == [tip]
    try:
        p.commit_files("w", tip, "again", {"x.json": b"{}"})
    except Exception as exc:
        assert "moved" in str(exc), exc
    else:
        _fail("a commit on a stale parent was accepted")
    assert "x.json" not in fake.files_at("w"), "the stale-parent commit did not land"
    print(
        "  wire: ONE multipart POST /src (branch+parents+message, /path fields); stale parent 409"
    )

    # ---- (34b) no Location header on the 201: the id is read back from the branch, but only
    # accepted when that head is a child of the parent we built on (a concurrent push between
    # the commit and the read-back is never recorded as ours)
    fake = FakeBitbucket(_repo_files())
    fake.no_location = True
    p = prov(fake)
    fake.refs["w"] = fake.refs["main"]
    tip = fake.refs["main"]
    sha = p.commit_files("w", tip, "no location", {"n.json": b"{}"})
    assert sha == fake.refs["w"] and fake.commits[sha]["parents"] == [tip]
    fake2 = FakeBitbucket(_repo_files())
    fake2.no_location = True
    p2 = prov(fake2)
    fake2.refs["w"] = fake2.refs["main"]
    tip2 = fake2.refs["main"]
    real_commit = fake2._commit

    def racing(body):
        resp = real_commit(body)
        fake2.refs["w"] = fake2._make_commit(
            dict(fake2.trees[fake2.commits[fake2.refs["w"]]["tree"]]), [fake2.refs["w"]], "intruder"
        )
        return resp

    fake2._commit = racing
    try:
        p2.commit_files("w", tip2, "no location", {"n.json": b"{}"})
    except Exception as exc:
        assert "moved right after" in str(exc), exc
    else:
        _fail("a head that is not our commit was recorded as ours")
    print("  wire: no Location -> read-back head must be our child; a racing push is refused")

    # ---- (35) a PUT that insists on a title is retried with the CURRENT title; description only
    # otherwise; a reviewer's title is kept either way
    fake = FakeBitbucket(_repo_files())
    c = _conn("strictput", {"target_locales": "fr-FR"})
    ok, msg, _r = _run(c, fake)
    assert ok and len(fake.prs) == 1, msg
    fake.prs[0]["title"] = "Reviewer title"
    fake.strict_put = True
    fake.commit_to("main", {"locales/en-US.json": _json(dict(EN_JSON, hello="Hello, strict"))})
    ok, msg, _r = _run(c, fake)
    assert ok and fake.prs[0]["title"] == "Reviewer title", (msg, fake.prs[0]["title"])
    assert BODY_BEGIN in fake.prs[0]["body"] and "locales/fr-FR.json" in fake.prs[0]["body"]
    puts = [w for w in fake.writes() if w[0] == "PUT"]
    assert puts and "title" not in puts[0][2], "the first attempt sends the description only"
    print(
        "  PR: description-only PUT; a title-demanding host gets the current title back unchanged"
    )

    # ---- (36) symlink / submodule / directory targets over a paginated listing
    fake = FakeBitbucket(_repo_files())
    fake.max_pagelen = 2
    fake.commit_to("main", {"locales/fr-FR.json": b"../x.json", "locales/de-DE.json": b"sha1"})
    fake.commit_to("main", {"locales/it-IT.json/inner.txt": b"dir in the way"})
    fake.modes["locales/fr-FR.json"] = "link"
    fake.modes["locales/de-DE.json"] = "subrepository"
    c = _conn("kinds", {"target_locales": "fr-FR, de-DE, it-IT, es-ES"})
    ok, msg, res = _run(c, fake)
    errs = " ".join(f["error"] for f in res["failures"])
    for want in (
        "locales/fr-FR.json is a symlink",
        "locales/de-DE.json is a submodule",
        "locales/it-IT.json is a directory",
    ):
        assert want in errs, (want, errs)
    files = fake.files_at(f"loc/{c.id}")
    assert files["locales/fr-FR.json"] == b"../x.json" and files["locales/de-DE.json"] == b"sha1"
    assert "locales/es-ES.json" in files, "the healthy locale still lands"
    print("  A: symlink / submodule / directory targets are refused per pair, others still land")

    # ---- (37) LFS or a moved file (raw read answers a redirect) is refused, not followed
    fake = FakeBitbucket(_repo_files())
    fake.lfs.add("locales/en-US.json")
    pr = prov(fake)
    try:
        pr.get_file(fake.refs["main"], "locales/en-US.json")
    except Exception as exc:
        assert "LFS" in str(exc), exc
    else:
        _fail("a redirect on a raw read was followed")
    assert fake.hosts == {"api.bitbucket.org"}
    print("  A: a raw read that redirects (LFS) is refused and never followed")

    # ---- (38) ancestry through commits?exclude=; no ref-update call exists
    fake = FakeBitbucket(_repo_files())
    pr = prov(fake)
    a = fake.refs["main"]
    fake.refs["x"] = a
    b = fake.commit_to("x", {"q.json": b"{}"})
    side = fake.commit_to("main", {"r.json": b"{}"})
    assert pr.is_ancestor(a, b) and pr.is_ancestor(a, a) and pr.is_ancestor(b, b)
    assert not pr.is_ancestor(b, a) and not pr.is_ancestor(b, side) and not pr.is_ancestor(side, b)
    try:
        pr.is_ancestor("main", b)
    except git_core.GitRefused:
        pass
    else:
        _fail("a branch name was accepted as a revision")
    try:
        pr.fast_forward("x", a)
    except Exception:
        pass
    else:
        _fail("fast_forward must not exist on Bitbucket")
    assert pr.can_fast_forward is False
    print("  provider: ancestry via commits?exclude=, revisions are full commit ids only")

    # ---- (39) 429 goes through base._request backoff and the run still completes
    fake = FakeBitbucket(_repo_files())
    c = _conn("ratelimit", {"target_locales": "fr-FR"})
    slept: list = []
    real_sleep = cbase._sleep
    cbase._sleep = lambda s: slept.append(s)
    fake.rate_limited = 2
    try:
        ok, msg, _r = _run(c, fake)
    finally:
        cbase._sleep = real_sleep
    assert ok and slept, (msg, slept)
    fake = FakeBitbucket(_repo_files())
    fake.rate_limited = 500
    cbase._sleep = lambda s: None
    try:
        ok, msg, _r = _run(_conn("ratelimit2", {"target_locales": "fr-FR"}), fake)
    finally:
        cbase._sleep = real_sleep
    assert not ok and "429" in msg and not fake.prs, msg
    print("  rate limit: 429 retried with backoff; a persistent 429 fails loud, nothing written")

    # ---- (40) a token-shaped string echoed back by the host never reaches a message
    fake = FakeBitbucket(_repo_files())
    fake.commit_error = True
    fake.token_echo = "ATATT3xOTHERpersonsTOKEN0123456789abcdef"  # pregate: allow-secret fixture
    c = _conn("echo", {"target_locales": "fr-FR"})
    ok, msg, res = _run(c, fake)
    assert not ok and "OTHERpersonsTOKEN" not in msg + json.dumps(res, default=str), msg
    print("  H: an Atlassian-token-shaped string in a host error is masked")

    # ---- (42) an email with a line break or colon is refused before anything is sent
    fake = FakeBitbucket(_repo_files())
    for evil in ("a@b.test\r\nX-Evil: 1", "a:b@c.test", "a b@c.test"):
        pe = prov(fake, email=evil)
        ok, msg = pe.c.test()
        assert not ok and "email" in msg and not fake.log, (evil, msg)
    print("  H: an email with a line break, colon or space is refused, nothing sent")

    # ---- (41) only a MATCHING push restriction blocks the working branch
    fake = FakeBitbucket(_repo_files())
    fake.admin = True
    fake.restrictions = [
        {"kind": "push", "branch_match_kind": "glob", "pattern": "release/*"},
        {"kind": "force", "branch_match_kind": "glob", "pattern": "loc/*"},
        {"kind": "push", "branch_match_kind": "branching_model", "branch_type": "development"},
    ]
    c = _conn("restrict", {"target_locales": "fr-FR"})
    ok, msg, _r = _run(c, fake)
    assert ok and len(fake.prs) == 1, msg
    print("  H: only matching push restrictions block the working branch")

    # ---- (43) an EXECUTABLE or Git LFS target is refused, never overwritten (an upload without
    # x-attributes would silently drop the executable bit / replace the LFS pointer)
    fake = FakeBitbucket(_repo_files())
    fake.commit_to("main", {"locales/fr-FR.json": b'{"x":"exec"}', "locales/de-DE.json": b"lfs"})
    fake.modes["locales/fr-FR.json"] = "executable"
    fake.lfs.add("locales/de-DE.json")
    c = _conn("execlfs", {"target_locales": "fr-FR, de-DE, es-ES"})
    ok, msg, res = _run(c, fake)
    errs = " ".join(f["error"] for f in res["failures"])
    for want in (
        "locales/fr-FR.json is a file marked executable",
        "locales/de-DE.json is a Git LFS file",
    ):
        assert want in errs, (want, errs)
    files = fake.files_at(f"loc/{c.id}")
    assert files["locales/fr-FR.json"] == b'{"x":"exec"}' and files["locales/de-DE.json"] == b"lfs"
    assert fake.modes.get("locales/fr-FR.json") == "executable", "the executable bit was dropped"
    assert "locales/es-ES.json" in files, "the healthy locale still lands"
    print("  A: an executable or Git LFS target is refused per pair, its mode and pointer kept")

    # ---- (44) every URL the token could ride on is pinned like git_core.validate_api_url:
    # next links that start with the repository prefix but carry dot segments (plain or
    # encoded), a backslash, a fragment or whitespace are refused; a Location header on another
    # host (or another repository) is never trusted for the commit id
    from app.services.connectors import bitbucket as bbmod

    pre = f"https://api.bitbucket.org/2.0/repositories/{REPO}/"
    for bad in (
        pre + "../../../users/me",
        pre + "src/%2e%2e/%2E%2E/x",
        pre + "src\\@evil.example/x",
        pre + "src/x#@evil.example",
        pre + "src/x y",
        "https://api.bitbucket.org:8443/2.0/repositories/" + REPO + "/src/x",
        "https://api.bitbucket.org.evil.example/2.0/repositories/" + REPO + "/src/x",
        "http://api.bitbucket.org/2.0/repositories/" + REPO + "/src/x",
    ):
        assert not bbmod._pinned_api_url(bad) or not bad.startswith(pre), bad
        fake = FakeBitbucket(_repo_files())
        fake.evil_next = bad
        fake.max_pagelen = 5
        try:
            prov(fake).list_files(fake.refs["main"], "locales")
        except Exception as exc:
            assert "outside this repository" in str(exc), (bad, exc)
        else:
            _fail(f"a next link was followed: {bad!r}")
        assert fake.hosts == {"api.bitbucket.org"}, fake.hosts
    assert bbmod._pinned_api_url(pre + "src/abc/?pagelen=5&page=2")
    for loc in (
        "https://evil.example/2.0/repositories/%s/commit/%s" % (REPO, "e" * 40),
        "https://api.bitbucket.org/2.0/repositories/other/repo/commit/%s" % ("e" * 40),
        "https://api.bitbucket.org@evil.example/2.0/repositories/%s/commit/%s" % (REPO, "e" * 40),
    ):
        fake = FakeBitbucket(_repo_files())
        fake.location_override = loc
        p = prov(fake)
        fake.refs["w"] = fake.refs["main"]
        tip = fake.refs["main"]
        sha = p.commit_files("w", tip, "pinned location", {"n.json": b"{}"})
        assert sha == fake.refs["w"] and sha != "e" * 40, (loc, sha)
    print("  H: next links and the Location header are pinned to api.bitbucket.org/<repo>")


if __name__ == "__main__":
    main()
