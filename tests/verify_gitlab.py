"""GitLab connector on the shared git-provider core: fully offline via httpx.MockTransport.

FakeGitLab models the parts of GitLab's REST v4 the connector relies on, with the platform's
REAL behaviour (taxonomy G): the commits API has no compare-and-swap, creating an existing
branch is a 400, a duplicate open merge request is a 409, the project is found by its
URL-encoded path and addressed by numeric id afterwards, the tree is keyset-paginated
through a Link header, and there is no call that moves a branch forward. Every request is
logged, so the safety rules can be asserted on the wire: no write ever touches the base
branch, one run is one commit, and the token only ever appears in the PRIVATE-TOKEN header.

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

ASCII-only prints; final line is 'verify_gitlab OK'.
"""

# ruff: noqa: E501, UP031
import base64
import hashlib
import json
import logging
import os
import re
import sys
from urllib.parse import parse_qs, quote, unquote

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_gitlab_")

import httpx
from sqlmodel import Session, select

from app import config as app_config
from app.db import engine, init_db
from app.models import Connection, Segment, SegStatus, SyncLog
from app.services import key_files, translate
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError
from app.services.connectors.gitlab import GitLabConnector

# Token prefixes are assembled at runtime so secret scanners do not flag the fixtures.
_PAT = "glp" + "at-"
_DEPLOY = "gld" + "t-"
TOKEN = _PAT + "FAKE_TOKEN_for_tests_0123456789"
REPO = "acme/team/site"  # a subgroup project: the GitLab-only path shape


def _sha(*parts) -> str:
    h = hashlib.sha1()  # noqa: S324 (fake git object ids, not security)
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class FakeGitLab:
    """An in-memory GitLab project behind httpx.MockTransport (REST v4).

    Models what the connector relies on, with GitLab's real behaviour where it matters
    (taxonomy G): the commits API has NO compare-and-swap (a commit lands on whatever the
    branch head is, with that head as its parent), create/update actions are validated against
    the tree, creating an existing branch is a 400, a duplicate open merge request is a 409, the
    project is addressed by its URL-encoded path first and by numeric id afterwards, the tree is
    keyset-paginated through a `Link: rel="next"` header, a symlink's raw content is the link
    target, and there is no call that moves a branch forward. Every request is logged so the
    safety rules are asserted on the wire."""

    PID = 4242

    def __init__(self, files=None, tree_limit=None, token=TOKEN, push=True):
        self.token = token
        self.push = push
        self.default_branch = "main"
        self.tree_limit = tree_limit
        self.blobs: dict = {}
        self.trees: dict = {}  # tree sha -> {path: blob sha}
        self.commits: dict = {}  # commit sha -> {tree, parents, message}
        self.refs: dict = {}  # branch -> commit sha
        self.prs: list = []  # merge requests (number = iid)
        self.log: list = []  # (method, path relative to the project, body)
        self.n_commits = 0
        self.before_patch = None  # hook(self, branch) run just before a commit lands
        self.noise_prs = 0
        self.fork_mrs = False  # an open MR from a FORK with the same source branch name
        self.pr_pages: list = []
        self.blob_error = False  # POST commits answers 400 and quotes the token
        self.big_file_paths: set = set()
        self.full_name = REPO  # what the project lookup answers
        self.archived = False
        self.pr_fail = False  # POST merge_requests answers 500 (the commit already landed)
        self.protected: set = set()  # branches the host reports as protected
        self.protected_rules: list = []  # protected-branch rules (names or wildcards)
        self.rules_forbidden = False  # GET protected_branches answers 403 (token cannot list)
        self.modes: dict = {}  # path -> git mode other than 100644
        self.delete_on_merge = True  # honour remove_source_branch when an MR is merged
        self.redirect = False  # the project lookup answers 301 (a renamed project)
        self.evil_link = False  # the tree Link header points at another host
        self.throttle = 0  # answer this many GETs with 429 + Retry-After first
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

    def _resolve(self, ref: str):
        return self.refs.get(ref) or (ref if ref in self.commits else None)

    def merge_pr(self, number: int) -> None:
        """Test helper: a human merges the MR (fast-forward the base to the MR head). GitLab
        deletes the source branch when the MR asked for it."""
        pr = next(p for p in self.prs if p["number"] == number)
        self.refs[pr["base"]] = self.refs[pr["head"]]
        pr["state"], pr["merged"] = "closed", True
        if pr.get("remove_source_branch") and self.delete_on_merge:
            self.refs.pop(pr["head"], None)

    # ---------------------------------------------------------------- the API
    def _json(self, code, obj, headers=None):
        return httpx.Response(code, json=obj, headers=headers or {})

    def _handle(self, req: httpx.Request) -> httpx.Response:
        raw = req.url.raw_path.decode("ascii")
        rawpath, _, rawq = raw.partition("?")
        assert rawpath.startswith("/api/v4/"), rawpath
        path = rawpath[len("/api/v4") :]
        body = json.loads(req.content) if req.content else None
        q = dict(parse_qs(rawq, keep_blank_values=True))
        q1 = {k: v[0] for k, v in q.items()}
        for hdr, val in req.headers.items():
            if hdr.lower() != "private-token" and self.token in val:
                return self._json(400, {"message": "token leaked in a header"})
        if self.token in str(req.url):
            return self._json(400, {"message": "token leaked in the url"})
        if req.headers.get("private-token", "") != self.token:
            self.log.append((req.method, path, body))
            return self._json(401, {"message": "401 Unauthorized"})
        if self.throttle and req.method == "GET":
            self.throttle -= 1
            self.log.append((req.method, path, body))
            return httpx.Response(429, json={"message": "slow down"}, headers={"Retry-After": "1"})
        m = req.method
        enc = quote(REPO, safe="")
        if path == f"/projects/{enc}" and m == "GET":
            self.log.append((m, path, body))
            if self.redirect:
                return httpx.Response(301, headers={"Location": "https://gitlab.test/elsewhere"})
            return self._json(
                200,
                {
                    "id": self.PID,
                    "path_with_namespace": self.full_name,
                    "archived": self.archived,
                    "default_branch": self.default_branch,
                    "permissions": {
                        "project_access": {"access_level": 30 if self.push else 20},
                        "group_access": None,
                    },
                },
            )
        prefix = f"/projects/{self.PID}"
        if not path.startswith(prefix):
            self.log.append((m, path, body))
            return self._json(404, {"message": "404 Project Not Found"})
        rest = path[len(prefix) :]
        self.log.append((m, rest, body))
        mm = re.match(r"^/repository/branches/([^/]+)$", rest)
        if mm and m == "GET":
            b = unquote(mm.group(1))
            if b not in self.refs:
                return self._json(404, {"message": "404 Branch Not Found"})
            return self._json(
                200,
                {"name": b, "protected": b in self.protected, "commit": {"id": self.refs[b]}},
            )
        if rest == "/protected_branches" and m == "GET":
            if self.rules_forbidden:
                return self._json(403, {"message": "403 Forbidden"})
            rules = [{"name": n} for n in sorted(self.protected | set(self.protected_rules))]
            per, page = int(q1.get("per_page", 20)), int(q1.get("page", 1))
            return self._json(200, rules[(page - 1) * per : page * per])
        if not self.push and m != "GET":
            return self._json(403, {"message": "403 Forbidden"})
        if rest == "/repository/branches" and m == "POST":
            b, ref = (body or {}).get("branch", ""), (body or {}).get("ref", "")
            if not b or ".." in b or " " in b:
                return self._json(400, {"message": "Branch name is invalid"})
            if b in self.refs:
                return self._json(400, {"message": "Branch already exists"})
            sha = self._resolve(ref)
            if sha is None:
                return self._json(400, {"message": "Invalid reference name"})
            self.refs[b] = sha
            return self._json(201, {"name": b, "commit": {"id": sha}, "protected": False})
        if rest == "/repository/tree" and m == "GET":
            return self._tree(q1)
        mm = re.match(r"^/repository/files/([^/]+?)(/raw)?$", rest)
        if mm and m == "GET":
            return self._file(unquote(mm.group(1)), bool(mm.group(2)), q1.get("ref"))
        if rest == "/repository/commits" and m == "POST":
            return self._commit(body or {})
        if rest == "/repository/merge_base" and m == "GET":
            refs = q.get("refs[]") or []
            if len(refs) != 2 or any(r not in self.commits for r in refs):
                return self._json(404, {"message": "404 Commit Not Found"})
            a, b = refs
            common = self._ancestors(a) & self._ancestors(b)
            if not common:
                return self._json(404, {"message": "404 Not Found"})
            if a in common and a in self._ancestors(b):
                return self._json(200, {"id": a})
            if b in self._ancestors(a):
                return self._json(200, {"id": b})
            # the nearest common ancestor: walk b's history breadth first
            todo = [b]
            while todo:
                c = todo.pop(0)
                if c in self._ancestors(a):
                    return self._json(200, {"id": c})
                todo.extend(self.commits[c]["parents"])
            return self._json(404, {"message": "404 Not Found"})
        if rest == "/merge_requests" and m == "GET":
            return self._mr_list(q1)
        if rest == "/merge_requests" and m == "POST" and self.pr_fail:
            return self._json(500, {"message": "Server Error"})
        if rest == "/merge_requests" and m == "POST":
            h, b = body["source_branch"], body["target_branch"]
            if h not in self.refs or b not in self.refs:
                return self._json(400, {"message": "source or target branch does not exist"})
            if any(p["state"] == "open" and p["head"] == h and p["base"] == b for p in self.prs):
                return self._json(
                    409, {"message": f"Another open merge request already exists for {h}"}
                )
            pr = {
                "number": len(self.prs) + 1,
                "head": h,
                "base": b,
                "state": "open",
                "merged": False,
                "title": body["title"],
                "body": body.get("description", ""),
                "remove_source_branch": bool(body.get("remove_source_branch")),
            }
            self.prs.append(pr)
            return self._json(201, self._pr_json(pr))
        mm = re.match(r"^/merge_requests/(\d+)$", rest)
        if mm:
            pr = next((p for p in self.prs if p["number"] == int(mm.group(1))), None)
            if pr is None:
                return self._json(404, {"message": "404 Not found"})
            if m == "PUT":
                for k, name in (("title", "title"), ("body", "description")):
                    if name in body:
                        pr[k] = body[name]
                if body.get("state_event") == "close" and pr["state"] == "open":
                    pr["state"] = "closed"
            return self._json(200, self._pr_json(pr))
        return self._json(404, {"message": f"unexpected {m} {rest}"})

    def _pr_json(self, p: dict, project_id=None) -> dict:
        return {
            "iid": p["number"],
            "state": "merged"
            if p.get("merged")
            else ("opened" if p["state"] == "open" else "closed"),
            "title": p["title"],
            "description": p.get("body", ""),
            "web_url": f"https://gitlab.test/{REPO}/-/merge_requests/{p['number']}",
            "source_branch": p["head"],
            "target_branch": p["base"],
            "source_project_id": project_id or self.PID,
            "target_project_id": self.PID,
        }

    def _mr_list(self, q: dict) -> httpx.Response:
        real = [
            self._pr_json(p)
            for p in self.prs
            if p["state"] == ("open" if q.get("state") == "opened" else p["state"])
            and p["head"] == q.get("source_branch", p["head"])
            and p["base"] == q.get("target_branch", p["base"])
        ]
        fork = []
        if self.fork_mrs:
            fork = [
                self._pr_json(
                    {
                        "number": 900,
                        "head": q.get("source_branch", ""),
                        "base": q.get("target_branch", "main"),
                        "state": "open",
                        "title": "from a fork",
                    },
                    project_id=999,
                )
            ]
        noise = [
            self._pr_json(
                {
                    "number": 1000 + i,
                    "head": q.get("source_branch", "x"),  # a server that ignored the filter
                    "base": "main",
                    "state": "open",
                    "title": "x",
                }
            )
            for i in range(self.noise_prs)
        ]
        for d in noise:
            d["source_branch"] = "noise"
        rows = noise + fork + real
        per, page = int(q.get("per_page", 20)), int(q.get("page", 1))
        self.pr_pages.append(page)
        return self._json(200, rows[(page - 1) * per : page * per])

    # ----------------------------------------------------------- repository
    def _entries(self, sha: str) -> list:
        flat = self.trees[self.commits[sha]["tree"]]
        out, dirs = [], set()
        for p in sorted(flat):
            parts = p.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
            mode = self.modes.get(p, "100644")
            out.append(
                {
                    "id": flat[p],
                    "name": parts[-1],
                    "type": "commit" if mode == "160000" else "blob",
                    "path": p,
                    "mode": mode,
                }
            )
        for d in sorted(dirs):
            out.append(
                {
                    "id": _sha("dir", sha, d),
                    "name": d.split("/")[-1],
                    "type": "tree",
                    "path": d,
                    "mode": "040000",
                }
            )
        return sorted(out, key=lambda e: e["path"])

    def _tree(self, q: dict) -> httpx.Response:
        sha = self._resolve(q.get("ref") or self.default_branch)
        if sha is None:
            return self._json(404, {"message": "404 Tree Not Found"})
        entries = self._entries(sha)
        path = q.get("path", "").strip("/")
        if path:
            entries = [e for e in entries if e["path"].startswith(path + "/")]
            if not entries:
                return self._json(404, {"message": "404 Tree Not Found"})
        if q.get("recursive") != "true":
            depth = path.count("/") + 2 if path else 1
            entries = [e for e in entries if e["path"].count("/") + 1 == depth]
        per = int(q.get("per_page", 20))
        if self.tree_limit:
            per = min(per, self.tree_limit)
        start = int(q.get("cursor", 0))
        page = entries[start : start + per]
        headers = {}
        if start + per < len(entries):
            host = "https://evil.example" if self.evil_link else "https://gitlab.test"
            nxt = dict(q, cursor=start + per, pagination="keyset")
            qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in nxt.items())
            headers["Link"] = (
                f'<{host}/api/v4/projects/{self.PID}/repository/tree?{qs}>; rel="next"'
            )
        return self._json(200, page, headers)

    def _last_touch(self, sha: str, p: str):
        """The newest commit on the first-parent chain from `sha` that changed file `p`."""
        while sha:
            flat = self.trees[self.commits[sha]["tree"]]
            parents = self.commits[sha]["parents"]
            prev = self.trees[self.commits[parents[0]]["tree"]] if parents else {}
            if flat.get(p) != prev.get(p):
                return sha
            sha = parents[0] if parents else None
        return None

    def _file(self, p: str, raw: bool, ref) -> httpx.Response:
        sha = self._resolve(ref or self.default_branch)
        if sha is None:
            return self._json(404, {"message": "404 Commit Not Found"})
        flat = self.trees[self.commits[sha]["tree"]]
        if p not in flat:
            return self._json(404, {"message": "404 File Not Found"})
        data = self.blobs[flat[p]]
        if raw:
            return httpx.Response(200, content=data)
        size = 11 * 1024 * 1024 if p in self.big_file_paths else len(data)
        return self._json(
            200,
            {
                "file_name": p.split("/")[-1],
                "file_path": p,
                "size": size,
                "encoding": "base64",
                "content": base64.b64encode(data).decode(),
                "blob_id": flat[p],
                "last_commit_id": self._last_touch(sha, p) or sha,
            },
        )

    def _commit(self, body: dict) -> httpx.Response:
        b = body.get("branch")
        if self.before_patch:
            hook, self.before_patch = self.before_patch, None
            hook(self, b)
        if self.blob_error:
            return self._json(400, {"message": f"rejected content for token {self.token}"})
        if b not in self.refs:
            return self._json(400, {"message": "branch does not exist; give start_branch"})
        if "force" in body or "start_sha" in body or "start_branch" in body:
            # GitLab rejects start_sha on an existing branch ("already exists") unless forced
            return self._json(400, {"message": "force/start_sha/start_branch are not accepted"})
        head = self.refs[b]
        flat = dict(self.trees[self.commits[head]["tree"]])
        for a in body.get("actions") or []:
            fp = a.get("file_path", "")
            if ".." in fp.split("/") or fp.startswith("/"):
                return self._json(400, {"message": "file_path is invalid"})
            if a.get("action") == "create" and fp in flat:
                return self._json(400, {"message": f"A file with this name already exists: {fp}"})
            if a.get("action") == "update" and fp not in flat:
                return self._json(400, {"message": f"A file with this name doesn't exist: {fp}"})
            if a.get("action") not in ("create", "update"):
                return self._json(400, {"message": "unsupported action in this fake"})
            lc = a.get("last_commit_id")
            # Files::MultiService: the file's last commit on the branch must equal its last
            # commit as of last_commit_id
            if (
                a.get("action") == "update"
                and lc
                and self._last_touch(head, fp) != self._last_touch(lc, fp)
            ):
                return self._json(
                    400, {"message": f"The file has changed since you started editing it: {fp}"}
                )
            data = (
                base64.b64decode(a["content"])
                if a.get("encoding") == "base64"
                else a["content"].encode("utf-8")
            )
            flat[fp] = self._blob(data)
        csha = self._make_commit(flat, [head], body.get("commit_message", ""))
        self.refs[b] = csha  # no compare-and-swap: lands on whatever the head is now
        return self._json(201, {"id": csha, "parent_ids": [head]})


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
        "base_url": "https://gitlab.test",
        "base_branch": "main",
        "source_paths": "locales/{locale}.json, po/{locale}.po",
        "source_locale": "en-US",
        "target_locales": "fr-FR, de-DE",
    }
    cfg.update(cfg_extra or {})
    return cstore.create("gitlab", name, cfg, secret or {"token": TOKEN}, actor="admin")


def _run(conn, fake, **kw):
    kw.setdefault("actor", "auto")
    return cstore.gitlab_translate(conn.id, transport=fake.transport(), **kw)


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
    assert GitLabConnector.capability.topology == "T3"

    # ---- (1) provider basics: test(), default branch, create_branch 422 on an existing one
    fake = FakeGitLab(_repo_files())
    cx = GitLabConnector(
        {
            "repo": REPO,
            "base_url": "https://gitlab.test",
            "source_paths": "locales/{locale}.json",
        },
        {"token": TOKEN},
        transport=fake.transport(),
    )
    cx.connection_id = 1
    ok, msg = cx.test()
    assert ok and REPO in msg, msg
    prov = cx.provider()
    assert prov.default_branch() == "main"
    fake.refs["loc/1"] = fake.refs["main"]
    assert prov.create_branch("loc/1", fake.refs["main"]) == fake.refs["main"], (
        "existing branch -> its head"
    )
    bad = GitLabConnector(
        {
            "repo": REPO,
            "base_url": "https://gitlab.test",
            "source_paths": "locales/{locale}.json",
        },
        {"token": "wrong-token-123456"},
        transport=fake.transport(),
    )
    bad.connection_id = 1
    ok, msg = bad.test()
    assert not ok and "401" in msg and "wrong-token-123456" not in msg, msg
    ro = FakeGitLab(_repo_files(), push=False)
    cro = GitLabConnector(
        {
            "repo": REPO,
            "base_url": "https://gitlab.test",
            "source_paths": "locales/{locale}.json",
        },
        {"token": TOKEN},
        transport=ro.transport(),
    )
    cro.connection_id = 1
    ok, msg = cro.test()
    assert not ok and "cannot push" in msg, msg
    print(
        "  provider: test(), 401 scrubbed, read-only token reported, create_branch tolerates an existing branch (400)"
    )

    # ---- (2) first run: JSON nested + PO + plurals, ONE commit, ONE pull request
    fake = FakeGitLab(_repo_files())
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
    hashes = _cfg(c.id).get("gitlab_hashes") or {}
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

    # ---- (5) merged PR: the branch is fast-forwarded to base, a NEW pull request opens
    fake.merge_pr(1)
    assert branch not in fake.refs, "remove_source_branch: GitLab deleted the merged branch"
    en3 = dict(EN_JSON, hello="Hello a third time")
    fake.commit_to("main", {"locales/en-US.json": _json(en3)}, "edit source again")
    ok, msg, _res4 = _run(c, fake)
    assert ok, msg
    assert len(fake.prs) == 2 and fake.prs[1]["state"] == "open", fake.prs
    assert fake.refs[branch] != fake.refs["main"] and fake.prs[1]["head"] == branch
    assert all((w[2] or {}).get("branch") != "main" for w in fake.writes()), "never main"
    print("  merged MR (source branch deleted by GitLab) -> branch recreated from base, a fresh MR")

    # ---- (6) H: the base branch is never written, on the wire
    for m, p, b in fake.writes():
        assert (b or {}).get("branch") != "main", (m, p)
        assert (b or {}).get("target_branch", "main") == "main", (m, p)
        assert "force" not in (b or {}), (m, p, b)
    print("  H: no wire request ever targeted main as a branch to write; no force flag anywhere")

    _more(cap)
    _review(cap)
    _gl(cap)
    print("verify_gitlab OK")


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
    fake = FakeGitLab(_repo_files())
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
        hashes = _cfg(c.id).get("gitlab_hashes") or {}
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
    hashes = _cfg(c.id).get("gitlab_hashes") or {}
    assert "locales/en-US.json:fr-FR" in hashes, hashes
    assert "locales/en-US.json:de-DE" not in hashes, hashes
    _set_row("Hello there", "de-DE", status=SegStatus.APPROVED)
    ok, msg, res = _run(c, fake)
    assert ok and "hello" in json.loads(fake.files_at(f"loc/{c.id}")["locales/de-DE.json"]), msg
    assert "locales/en-US.json:de-DE" in (_cfg(c.id).get("gitlab_hashes") or {})
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
    fake = FakeGitLab({**_repo_files(), "locales/fr-FR.json": _json(existing)})
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
    fake = FakeGitLab(_repo_files())
    c = _conn("allheld", {"target_locales": "fr-FR"})
    restore = _patch_pretranslate(None, blank=True)
    try:
        ok, msg, res = _run(c, fake)
    finally:
        restore()
    assert not ok and not fake.writes() and not _cfg(c.id).get("gitlab_hashes"), (msg, res)
    assert f"loc/{c.id}" not in fake.refs, "no branch for an empty run"
    assert _status(c.id)[0] == "error"
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"], (msg, res)
    print("  F: nothing translated -> no branch, no commit, no hash; the healthy re-run completes")

    # ---- (10) A: hostile article ids are refused before any request
    fake = FakeGitLab(_repo_files())
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
        {"repo": "acme/team/site/../x"},
        {"repo": "acme/team//site"},
        {"repo": "acme/.hidden/site"},
        {"repo": "acme/team.atom/site"},
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
        {"base_url": "http://169.254.169.254"},
        {"base_url": "http://localhost:8080"},
        {"base_url": "http://127.0.0.1"},
        {"base_url": "http://gitlab.example.com"},
        {"base_url": "https://localhost"},
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
    ok, msg, res = _run(c, fake, target_locales="../../etc, fr-FR/../x")
    assert not [w for w in fake.writes() if ".." in w[1]], "locale override cannot traverse"
    print(
        "  A: %d hostile repo/branch/path/locale/host settings refused, zero wire traffic"
        % len(bad_cfgs)
    )

    # ---- (12) the base branch is never written, even when asked to (branch == default)
    fake = FakeGitLab(_repo_files())
    cx = _conn("tobase", {"branch_pattern": "main", "target_locales": "fr-FR"})
    ok, msg, _r = _run(cx, fake)
    assert not ok and not fake.writes() and "base" in msg.lower(), msg
    fake2 = FakeGitLab(_repo_files())
    cx = _conn("todefault", {"branch_pattern": "release/{connection}", "target_locales": "fr-FR"})
    fake2.default_branch = f"release/{cx.id}"
    fake2.refs[fake2.default_branch] = fake2.refs["main"]
    ok, msg, _r = _run(cx, fake2)
    assert not ok and not fake2.writes() and "default" in msg.lower(), msg
    print("  H: a working branch equal to the base or the repository default branch is refused")

    # ---- (13) G: a 422 on a branch that is not a fast-forward is loud, never an overwrite
    fake = FakeGitLab(_repo_files())
    c = _conn("race", {"target_locales": "fr-FR"})

    def stray(f, b):
        f.before_patch = None
        f.commit_to(b, {"stray.txt": b"someone else pushed"}, "stray")

    fake.before_patch = stray
    ok, msg, res = _run(c, fake)
    # GitLab's commits API has no compare-and-swap: our commit lands on top of the stray one
    # (additive, nothing overwritten), the provider sees the wrong parent and fails the run.
    assert not ok and "moved" in msg, msg
    assert fake.files_at(f"loc/{c.id}").get("stray.txt") == b"someone else pushed"
    assert not _cfg(c.id).get("gitlab_hashes"), "a failed push records no hash"
    assert not fake.prs, "no MR for a run that failed"
    ok, msg, res = _run(c, fake)
    # Gate 2026-10-10: the stray commit is someone else's, so the core never builds on it;
    # with no pull request open it starts a fresh branch from the base and leaves loc/N alone.
    stray_head = fake.refs[f"loc/{c.id}"]
    assert ok and "locales/fr-FR.json" in fake.files_at(f"loc/{c.id}-2"), (msg, sorted(fake.refs))
    assert fake.refs[f"loc/{c.id}"] == stray_head, "the stray branch is never written"
    assert "stray.txt" not in fake.files_at(f"loc/{c.id}-2")
    print(
        "  G: non-fast-forward 422 -> failed run, nothing overwritten, no hash; next run uses a new branch"
    )

    # ---- (14) pagination: truncated recursive tree + pull request lists
    many = {f"locales/lang{i:02d}/f{j}.json": b"{}" for i in range(6) for j in range(3)}
    fake = FakeGitLab({**_repo_files(), **many}, tree_limit=5)
    cx = GitLabConnector(
        {
            "repo": REPO,
            "base_url": "https://gitlab.test",
            "source_paths": "locales/{locale}.json",
        },
        {"token": TOKEN},
        transport=fake.transport(),
    )
    cx.connection_id = 1
    prov = cx.provider()
    got = {f.path for f in prov.list_files(fake.refs["main"], "locales") if f.kind == "file"}
    assert got, "listing returned files"
    want = {p for p in fake.files_at("main") if p.startswith("locales/")}
    assert got == want, (len(got), len(want))
    n_tree = len([1 for m, p, b in fake.log if m == "GET" and p == "/repository/tree"])
    assert n_tree > 3, ("keyset pages followed through the Link header", n_tree)
    c = _conn("trunc", {"target_locales": "fr-FR"})
    ok, msg, res = _run(c, fake)
    assert ok and res["created"] == 2, (msg, res)
    fake = FakeGitLab(_repo_files())
    fake.noise_prs = 150
    c = _conn("noise", {"target_locales": "fr-FR"})
    ok, _m, _r = _run(c, fake)
    assert ok and len(fake.prs) == 1
    fake.commit_to("main", {"locales/en-US.json": _json(dict(EN_JSON, hello="Another"))})
    ok, _m, _r = _run(c, fake)
    assert ok and len(fake.prs) == 1 and 2 in fake.pr_pages, ("PR found on page 2", fake.pr_pages)
    print("  pagination: keyset tree pages followed; the open MR is found past page 1")

    # ---- (15) H: WriteBudget ceiling (one unit per file); the rest stays pending
    fake = FakeGitLab(_repo_files())
    c = _conn("budget", {"target_locales": "fr-FR, de-DE"})
    saved = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        ok, msg, res = _run(c, fake)
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    assert res["ceiling_hit"] and res["created"] == 1 and len(res["failures"]) == 3, res
    assert len(_cfg(c.id).get("gitlab_hashes") or {}) == 1
    ok, msg, res = _run(c, fake)
    assert ok and res["created"] == 3 and not res["ceiling_hit"], (msg, res)
    print("  H: write ceiling stops after 1 file; the next run completes the other 3")

    # ---- (16) H: mock MT + real egress is refused (no external writes)
    fake = FakeGitLab(_repo_files())
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
    fake = FakeGitLab(_repo_files())
    fake.blob_error = True
    c = _conn("leak", {"target_locales": "fr-FR"})
    ok, msg, res = _run(c, fake)
    assert not ok and TOKEN not in msg and TOKEN not in json.dumps(res), msg
    fake.blob_error = False
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
        assert TOKEN not in path and TOKEN not in json.dumps(body or {}), (method, path)
    print(
        "  H: token absent from messages, results, logs, stored config, sync log, URLs and bodies"
    )

    # ---- (18) credential pin: a different repository or host clears the stored token
    base = {"repo": REPO, "source_paths": "a/{locale}.json"}
    c = cstore.create("gitlab", "pin", dict(base), {"token": TOKEN}, actor="a")
    cstore.update(c.id, "pin", {"base_url": "https://gitlab.com"}, None, actor="a")
    assert dec_secret(cstore.get(c.id).secret_enc).get("token") == TOKEN, "explicit default host"
    cstore.update(c.id, "pin", {"base_url": "https://gitlab.com/api/v4/"}, None, actor="a")
    assert dec_secret(cstore.get(c.id).secret_enc).get("token") == TOKEN, "explicit default host"
    cstore.update(c.id, "pin", {"base_branch": "dev"}, None, actor="a")
    assert dec_secret(cstore.get(c.id).secret_enc).get("token") == TOKEN
    cstore.update(c.id, "pin", {"repo": "acme/team/other"}, None, actor="a")
    assert not dec_secret(cstore.get(c.id).secret_enc).get("token"), "repo change clears the token"
    c = cstore.create("gitlab", "pin2", dict(base), {"token": TOKEN}, actor="a")
    cstore.update(c.id, "pin2", {"base_url": "https://gl.evil.example"}, None, actor="a")
    assert not dec_secret(cstore.get(c.id).secret_enc).get("token"), "host change clears the token"
    print("  H: repository / API host change clears the stored token; same-host edits keep it")

    # ---- (19) locale styles + removed keys + retry bypass + missing source
    fake = FakeGitLab({"i18n/en.json": _json({"a": "One", "b": "Two"})})
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
    fake = FakeGitLab(_repo_files())
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

    assert "gitlab" in REGISTRY
    tc = TestClient(app)
    assert "GitLab" in tc.get("/connectors").text
    typ = tc.get("/connectors/gitlab").text
    assert "Project" in typ and "Access token" in typ, "type form renders"
    from app.cli.__main__ import build_parser

    assert "connector gitlab-translate" in _leaf_paths(build_parser())
    assert "/api/connectors/{cid}/gitlab/translate" in {
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
    fake = FakeGitLab(_repo_files())
    fake.commit_to("main", {"locales/fr-FR.json": b"../shared/fr.json"})
    fake.modes["locales/fr-FR.json"] = "120000"
    c = _conn(
        "symlink", {"target_locales": "fr-FR, de-DE", "source_paths": "locales/{locale}.json"}
    )
    ok, msg, res = _run(c, fake)
    files = fake.files_at(f"loc/{c.id}")
    assert files["locales/fr-FR.json"] == b"../shared/fr.json", "the symlink was replaced"
    assert "locales/de-DE.json" in files, "the other locale still lands"
    assert any("symlink" in f["error"] for f in res["failures"]), res
    assert "locales/en-US.json:fr-FR" not in (_cfg(c.id).get("gitlab_hashes") or {}), "no hash"
    fake = FakeGitLab(_repo_files())
    fake.modes["locales/en-US.json"] = "120000"
    c = _conn("srclink", {"target_locales": "fr-FR", "source_paths": "locales/{locale}.json"})
    ok, msg, res = _run(c, fake)
    assert not fake.writes() and any("symlink" in f["error"] for f in res["failures"]), (msg, res)
    print(
        "  A: a symlink at a target path is not replaced; a symlinked source is refused, no writes"
    )

    # ---- (24) a protected working branch is refused before any write
    fake = FakeGitLab(_repo_files())
    c = _conn("prot", {"target_locales": "fr-FR"})
    fake.refs[f"loc/{c.id}"] = fake.refs["main"]
    fake.protected.add(f"loc/{c.id}")
    ok, msg, _r = _run(c, fake)
    assert not ok and "protected" in msg and not fake.writes(), msg
    print("  H: a protected working branch is refused, nothing written")

    # ---- (25) repository identity pinned in the answer; archived repo refused
    fake = FakeGitLab(_repo_files())
    fake.full_name = "evil/other"
    c = _conn("ident", {"target_locales": "fr-FR"})
    ok, msg, _r = _run(c, fake)
    assert not ok and "evil/other" in msg and not fake.writes(), msg
    fake = FakeGitLab(_repo_files())
    fake.archived = True
    ok, msg, _r = _run(_conn("arch", {"target_locales": "fr-FR"}), fake)
    assert not ok and "archived" in msg and not fake.writes(), msg
    print("  H: an answer for a different repository, or an archived one, is refused")

    # ---- (26) a malformed token is refused without being echoed or sent
    fake = FakeGitLab(_repo_files())
    weird = "ghp_SECRETPART one\ntwo_0123456789"  # pregate: allow-secret offline fixture
    c = _conn("badtok", {"target_locales": "fr-FR"}, {"token": weird})
    ok, msg, res = _run(c, fake)
    assert not ok and not fake.log and "SECRETPART" not in msg, msg
    assert not any("SECRETPART" in line for line in cap.lines), "token fragment in a log"
    print("  H: a token with spaces or line breaks is refused, never echoed, never sent")

    # ---- (27) commit landed but the PR open failed: the next run opens the PR, no new commit
    fake = FakeGitLab(_repo_files())
    c = _conn("orphan", {"target_locales": "fr-FR"})
    fake.pr_fail = True
    ok, msg, res = _run(c, fake)
    br = f"loc/{c.id}"
    assert not ok and br in fake.refs and not fake.prs, (msg, fake.prs)
    assert not _cfg(c.id).get("gitlab_hashes"), "no hash when the PR was not opened"
    fake.pr_fail = False
    head = fake.refs[br]
    ok, msg, res = _run(c, fake)
    assert ok and len(fake.prs) == 1 and fake.refs[br] == head, (msg, fake.prs)
    assert res["pull_request"]["number"] == 1 and not res["commit"], res
    assert len(_cfg(c.id).get("gitlab_hashes") or {}) == 2, _cfg(c.id)
    n = len(fake.writes())
    ok, msg, res = _run(c, fake)
    assert ok and res["skipped"] == 2 and len(fake.writes()) == n, "steady state afterwards"
    print("  E: an orphaned working branch gets its pull request on the next run (no new commit)")

    # ---- (28) workflow_id: the workflow path replaces the direct pretranslate and reports
    fake = FakeGitLab(_repo_files())
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


def _gate_branches() -> None:
    """Review gate 2026-10-10: branch ownership, base moved under an open PR, reviewer edits."""
    from app.services.connectors.git_core import BODY_BEGIN, BODY_END

    def edit_source(fake, hello):
        fake.commit_to("main", {"locales/en-US.json": _json(dict(EN_JSON, hello=hello))})

    # ---- (29) a branch with the working-branch name that this connection did not create
    fake = FakeGitLab(_repo_files())
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
        {"gitlab_branch_state": {"branch": br, "head": human, "base": human, "gen": 1}},
        None,
    )
    assert "gitlab_branch_state" not in _cfg(c.id), _cfg(c.id)
    ok, msg, _r = _run(c, fake)
    assert not ok and "not created by this connection" in msg and not fake.writes(), msg
    print(
        "  H: a same-named branch this connection did not create is refused; forged record dropped"
    )

    # ---- (30) reviewer edits to the PR title/body survive; base moved without touching
    # our files keeps the same branch and PR
    fake = FakeGitLab(_repo_files())
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
    assert not any(w[2] and "title" in w[2] for w in fake.writes() if "/merge_requests/" in w[1]), (
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
    assert len(fake.prs) == 2 and pr1["state"] == "closed" and not pr1.get("merged"), fake.prs
    pr2 = fake.prs[1]
    assert pr2["head"] == br2 and pr2["state"] == "open" and "#1" in pr2["body"], pr2
    assert res["pull_request"]["number"] == 2 and any("#1" in n for n in res["notes"]), res
    print("  base moved over our files: new branch from base, new PR names #1, #1 closed")

    # ---- (32) someone pushes to our open PR branch: the run refuses to write on top
    reviewer = fake.commit_to(br2, {"locales/fr-FR.json": b'{"hello": "Salut (relu)"}\n'})
    edit_source(fake, "Hello, after review")
    n = len(fake.writes())
    ok, msg, _r = _run(c, fake)
    assert not ok and "#2" in msg and "someone else" in msg, msg
    assert len(fake.writes()) == n and fake.refs[br2] == reviewer, "nothing written on top"
    # once that PR is merged, the next run starts a fresh branch and leaves br2 alone
    fake.delete_on_merge = False
    fake.merge_pr(2)
    edit_source(fake, "Hello, after merge")
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"] and f"loc/{c.id}-3" in fake.refs, (msg, sorted(fake.refs))
    assert fake.refs[br2] == reviewer and fake.prs[-1]["head"] == f"loc/{c.id}-3", fake.prs
    print("  reviewer commits on an open PR are never buried; after the merge a fresh branch")

    # ---- (33) PR closed without merging (or squash-merged), base moved: a new branch from
    # the base, the old branch untouched, the closed PR not reopened
    fake = FakeGitLab(_repo_files())
    c = _conn("squash", {"target_locales": "fr-FR"})
    br = f"loc/{c.id}"
    ok, msg, _r = _run(c, fake)
    assert ok and len(fake.prs) == 1, msg
    fake.prs[0]["state"] = "closed"
    head = fake.refs[br]
    edit_source(fake, "Hello, squash")
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"] and fake.refs[br] == head, (msg, res)
    assert f"loc/{c.id}-2" in fake.refs and len(fake.prs) == 2, (sorted(fake.refs), fake.prs)
    assert fake.prs[0]["state"] == "closed" and fake.prs[1]["state"] == "open", fake.prs
    assert set(fake.files_at(f"loc/{c.id}-2")) >= {"locales/fr-FR.json"}
    print("  closed/squashed PR + base moved: new branch from base, old one untouched")


def _gl(cap) -> None:
    """GitLab-specific blocks: what differs from GitHub on the wire and in the provider."""
    from app.services.connectors import base as conn_base
    from app.services.connectors import git_core

    def edit_source(fake, hello):
        fake.commit_to("main", {"locales/en-US.json": _json(dict(EN_JSON, hello=hello))})

    def provider(fake, **cfg):
        c = GitLabConnector(
            dict(
                {
                    "repo": REPO,
                    "base_url": "https://gitlab.test",
                    "source_paths": "locales/{locale}.json",
                },
                **cfg,
            ),
            {"token": TOKEN},
            transport=fake.transport(),
        )
        c.connection_id = 1
        return c, c.provider()

    # ---- (34) merged MR, branch kept: GitLab cannot move a branch forward -> fresh branch
    fake = FakeGitLab(_repo_files())
    fake.delete_on_merge = False
    c = _conn("keptbranch", {"target_locales": "fr-FR"})
    br = f"loc/{c.id}"
    ok, msg, _r = _run(c, fake)
    assert ok and fake.prs[0]["remove_source_branch"] is True, (msg, fake.prs)
    fake.merge_pr(1)
    merged_head = fake.refs[br]
    edit_source(fake, "Hello after a kept-branch merge")
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"] and fake.refs[br] == merged_head, (msg, res)
    assert f"loc/{c.id}-2" in fake.refs and fake.prs[-1]["head"] == f"loc/{c.id}-2", fake.prs
    assert not [w for w in fake.writes() if w[1].startswith("/repository/branches/")], (
        "no ref update call exists or is attempted"
    )
    print("  MR merged, source branch kept -> no fast-forward exists, fresh branch loc/N-2")

    # ---- (35) the https-only, self-managed URL; a pasted /api/v4 suffix is accepted
    fake = FakeGitLab(_repo_files())
    _cx, prov = provider(fake, base_url="https://gitlab.test/api/v4/")
    assert prov.default_branch() == "main" and prov._root == "https://gitlab.test/api/v4"
    for bad in (
        "http://gitlab.test",
        "ftp://gitlab.test",
        "gitlab.test",
        "https://localhost:8443",
        # the token must go to the host the operator can SEE: userinfo, query, fragment,
        # backslash and dot segments are refused (found in review: "https://gitlab.test@evil")
        "https://gitlab.test@evil.example",
        "https://user:pw@gitlab.test",
        "https://gitlab.test?x=1",
        "https://gitlab.test#frag",
        r"https://gitlab.test\@evil.example",
        "https://gitlab.test/%2e%2e/x",
        "https://gitlab.test/a b",
        "https://gitlab.test/%2E%2E/x",
        "https://gitlab.test/a/../b",
        "https://gitlab.test%5c@evil.example",
    ):
        cx2 = GitLabConnector(
            {"repo": REPO, "base_url": bad, "source_paths": "locales/{locale}.json"},
            {"token": TOKEN},
            transport=fake.transport(),
        )
        cx2.connection_id = 1
        n = len(fake.log)
        ok, msg = cx2.test()
        assert not ok and len(fake.log) == n and TOKEN not in msg, (bad, msg)
    print("  H: http / bare / loopback GitLab URLs refused before any request; /api/v4 accepted")

    # ---- (36) token shapes: a glpat- token echoed by the host is masked even if not ours
    fake = FakeGitLab(_repo_files())
    c = _conn("shape", {"target_locales": "fr-FR"})
    other = _PAT + "OTHERSECRETxyz0123456789"
    fake.blob_error = True
    real_commit = fake._commit

    def echo(body):
        r = real_commit(body)
        if r.status_code == 400:
            return httpx.Response(400, json={"message": f"bad token {other} and {TOKEN}"})
        return r

    fake._commit = echo
    ok, msg, res = _run(c, fake)
    assert not ok and other not in msg and TOKEN not in msg, msg
    assert other not in json.dumps(res) and not any(other in line for line in cap.lines)
    for weird in ("glpat-ab cd0123456789", "glpat-abc\n0123456789", "tok;en0123456789", ""):
        fake2 = FakeGitLab(_repo_files())
        c2 = _conn("shape2", {"target_locales": "fr-FR"}, {"token": weird or "x"})
        ok, msg, _r = _run(c2, fake2)
        assert not ok and not fake2.log and "abcd0123" not in msg, (weird, msg)
    print("  H: glpat- shapes scrubbed from messages and logs; malformed tokens never sent")

    # ---- (37) project path rules (subgroups), pinning, rename redirect, archived
    for ok_repo in ("acme/site", "acme/team/site", "a/b/c/d/e/site", "acme/my.site-1_x"):
        git_core.validate_repo(ok_repo, nested=True)
    for bad in (
        "acme",
        "/acme/site",
        "acme//site",
        "acme/../site",
        "acme/./site",
        "acme/site.git",
        "acme/.hidden/site",
        "acme/team.atom/site",
        "acme/si te",
        "acme/site?x=1",
        "acme/site%2Fx",
        "a/b/c/d/e/f/g/h/i/j/k",
        "acme/site\x00",
    ):
        try:
            git_core.validate_repo(bad, nested=True)
        except git_core.GitRefused:
            continue
        _fail(f"nested repo accepted: {bad!r}")
    for bad in ("acme/team/site", "acme"):  # GitHub keeps owner/name only
        try:
            git_core.validate_repo(bad)
        except git_core.GitRefused:
            continue
        _fail(f"owner/name accepted: {bad!r}")
    fake = FakeGitLab(_repo_files())
    fake.redirect = True
    ok, msg, _r = _run(_conn("renamed", {"target_locales": "fr-FR"}), fake)
    assert not ok and not fake.writes(), msg
    fake = FakeGitLab(_repo_files())
    _cx, prov = provider(fake)
    prov.project()
    assert prov._pp == f"/projects/{FakeGitLab.PID}", "later calls use the numeric id"
    prov.default_branch()
    assert all("%2F" not in p for m, p, b in fake.log[1:]), "no encoded path after the lookup"
    print("  A: nested project paths, rename redirect refused, numeric id after the pin")

    # ---- (38) a fork's merge request with our branch name is not ours
    fake = FakeGitLab(_repo_files())
    fake.fork_mrs = True
    c = _conn("forked", {"target_locales": "fr-FR"})
    ok, msg, res = _run(c, fake)
    assert ok and res["pull_request"]["number"] == 1, (msg, res)
    assert len(fake.prs) == 1 and fake.prs[0]["head"] == f"loc/{c.id}", fake.prs
    edit_source(fake, "Hello, fork test")
    ok, msg, res = _run(c, fake)
    assert ok and len(fake.prs) == 1, (msg, fake.prs)
    assert not [w for w in fake.writes() if "/merge_requests/900" in w[1]], "fork MR untouched"
    print("  MR lookup pins source and target project ids (a fork's MR is never updated)")

    # ---- (39) tree modes: 100755 is a file; any other mode is refused, never replaced
    fake = FakeGitLab(_repo_files())
    _cx, prov = provider(fake)
    fake.modes.update(
        {"locales/x.json": "100755", "locales/y.json": "100664", "locales/z": "160000"}
    )
    for name in ("x.json", "y.json", "z"):
        fake.commit_to("main", {f"locales/{name}": b"{}"})
    kinds = {f.path: f.kind for f in prov.list_files(fake.refs["main"], "locales")}
    from app.services.connectors import git_core

    assert kinds["locales/x.json"] == git_core.KIND_EXECUTABLE, kinds
    assert kinds["locales/y.json"] == "other", kinds
    assert kinds["locales/z"] == "submodule", kinds
    fake.commit_to("main", {"locales/fr-FR.json": b"{}"})
    fake.modes["locales/fr-FR.json"] = "100664"
    c = _conn("odd", {"target_locales": "fr-FR, de-DE"})
    ok, msg, res = _run(c, fake)
    files = fake.files_at(f"loc/{c.id}")
    assert files["locales/fr-FR.json"] == b"{}" and "locales/de-DE.json" in files, sorted(files)
    assert any("other" in f["error"] for f in res["failures"]), res
    print(
        "  A: 100755 is reported as executable; an unknown mode or a submodule is refused, not overwritten"
    )

    # ---- (40) keyset pagination: a Link to another host is not followed
    fake = FakeGitLab(
        {**_repo_files(), **{f"locales/l{i}/f.json": b"{}" for i in range(12)}}, tree_limit=4
    )
    _cx, prov = provider(fake)
    got = prov.list_files(fake.refs["main"], "locales")
    assert len([f for f in got if f.kind == "file"]) == 13, len(got)
    pages = [1 for m, p, b in fake.log if p == "/repository/tree"]
    assert len(pages) >= 4, ("followed the Link header", len(pages))
    fake.evil_link = True
    n = len(fake.log)
    try:
        prov.list_files(fake.refs["main"], "locales")
    except ConnectorError as e:
        assert "unexpected address" in str(e), e
    else:
        _fail("an off-host Link header was followed")
    assert len(fake.log) == n + 1, "only the first page was requested"
    print("  pagination: keyset Link followed page by page; an off-host Link is refused")

    # ---- (41) merge_base ancestry, raw bytes, create vs update actions
    fake = FakeGitLab(_repo_files())
    _cx, prov = provider(fake)
    base = fake.refs["main"]
    fake.refs["side"] = base
    side = fake.commit_to("side", {"a.txt": b"1"})
    other = fake.commit_to("main", {"b.txt": b"2"})
    assert prov.is_ancestor(base, side) and prov.is_ancestor(base, base)
    assert not prov.is_ancestor(side, base) and not prov.is_ancestor(side, other)
    fake.commit_to("main", {"locales/bin.json": b"\xff\xfe{}"})
    got = prov.get_file(fake.refs["main"], "locales/bin.json")
    assert got is not None and got[0] == b"\xff\xfe{}", "exact stored bytes via /raw"
    assert any(p.endswith("/raw") for m, p, b in fake.log), "the raw endpoint is used"
    fake.refs["w"] = fake.refs["main"]
    sha = prov.commit_files(
        "w", fake.refs["w"], "m", {"locales/bin.json": b"{}", "new/f.json": b"{}"}
    )
    acts = [a["action"] for m, p, b in fake.log if p == "/repository/commits" for a in b["actions"]]
    assert sorted(acts) == ["create", "update"], acts
    assert fake.refs["w"] == sha and "force" not in json.dumps([b for m, p, b in fake.log if b])
    n_commits = len([1 for m, p, b in fake.log if p == "/repository/commits"])
    try:
        prov.commit_files("w", base, "stale parent", {"q.json": b"{}"})
    except ConnectorError as e:
        assert "moved" in str(e)
        assert n_commits == len([1 for m, p, b in fake.log if p == "/repository/commits"]), (
            "a stale parent is refused BEFORE the commit is sent"
        )
    else:
        _fail("a commit on a stale parent went through")
    try:
        prov.fast_forward("w", base)
    except git_core.GitRefused:
        pass
    else:
        _fail("fast_forward must not exist on GitLab")
    print("  provider: merge_base ancestry, exact raw bytes, create/update actions, stale parent")

    # ---- (42) 429 + Retry-After is retried by the shared request path
    fake = FakeGitLab(_repo_files())
    fake.throttle = 2
    slept: list = []
    real_sleep = conn_base._sleep
    conn_base._sleep = lambda s: slept.append(s)
    try:
        ok, msg, res = _run(_conn("throttled", {"target_locales": "fr-FR"}), fake)
    finally:
        conn_base._sleep = real_sleep
    assert ok and slept and all(s >= 1 for s in slept), (msg, slept)
    print("  rate limit: 429 with Retry-After backs off and the run completes")

    # ---- (43) protected-branch rules: wildcard rule covers a branch that does not exist yet
    fake = FakeGitLab(_repo_files())
    fake.protected_rules = ["loc/*"]
    c = _conn("wild", {"target_locales": "fr-FR"})
    ok, msg, _r = _run(c, fake)
    assert not ok and "protected" in msg and not fake.writes(), msg
    fake = FakeGitLab(_repo_files())
    fake.rules_forbidden = True  # the token cannot list rules: GitLab itself enforces them
    ok, msg, _r = _run(_conn("norules", {"target_locales": "fr-FR"}), fake)
    assert ok, msg
    print("  H: a wildcard protected-branch rule refuses the working branch before any write")

    # ---- (44) the server-side precondition: update actions carry last_commit_id, so a target file
    # edited by someone else between our read and our commit is REJECTED by GitLab (not overwritten)
    fake = FakeGitLab(_repo_files())
    _cx, prov = provider(fake)
    fake.refs["w"] = fake.refs["main"]
    sha1 = prov.commit_files("w", fake.refs["w"], "first", {"locales/fr-FR.json": b'{"a":"1"}'})
    wire = [b for m, p, b in fake.log if p == "/repository/commits"][-1]
    assert "start_sha" not in wire and "force" not in wire and "start_branch" not in wire, wire
    assert all(a["action"] == "create" and "last_commit_id" not in a for a in wire["actions"])

    def same_file(f, b):
        f.before_patch = None
        f.commit_to(b, {"locales/fr-FR.json": b'{"a":"HUMAN EDIT"}'}, "human edit")

    fake.before_patch = same_file
    try:
        prov.commit_files("w", sha1, "second", {"locales/fr-FR.json": b'{"a":"2"}'})
    except ConnectorError as e:
        assert "moved" in str(e), e
    else:
        _fail("a concurrent edit of a target file was overwritten")
    assert fake.files_at("w")["locales/fr-FR.json"] == b'{"a":"HUMAN EDIT"}', "human edit kept"
    wire = [b for m, p, b in fake.log if p == "/repository/commits"][-1]
    assert [a.get("last_commit_id") for a in wire["actions"]] == [sha1], wire
    # a file created by someone else in the window: our create is rejected by GitLab too
    fake.refs["v"] = fake.refs["main"]
    base_v = fake.refs["v"]

    def same_new(f, b):
        f.before_patch = None
        f.commit_to(b, {"po/fr-FR.po": b"theirs"}, "human adds")

    fake.before_patch = same_new
    try:
        prov.commit_files("v", base_v, "create", {"po/fr-FR.po": b"ours"})
    except ConnectorError as e:
        assert "moved" in str(e), e
    else:
        _fail("a concurrently created file was overwritten")
    assert fake.files_at("v")["po/fr-FR.po"] == b"theirs"
    print(
        "  G: update carries last_commit_id; a concurrently edited/created target file is rejected"
    )

    # ---- (45) token shapes: every GitLab prefix is scrubbed, dotted routable tokens whole
    for tok in (
        _PAT + "abcdefghij0123456789",
        "glptt-abcdefghij0123456789",
        _DEPLOY + "abcdefghij0123456789",
        _PAT + "AbCdEfGhIjKlMnOpQr.01.0abcd1234",
    ):
        out = git_core._scrub(f"x {tok} y", [])
        assert tok[:8] not in out and "***" in out and ".01." not in out, (tok, out)
    assert git_core._scrub("ghp_abcdefghij0123456789", []) == "***", "GitHub shape unchanged"
    print("  H: glpat-/glptt-/gldt- (and dotted routable) tokens scrubbed; GitHub shapes unchanged")

    # ---- (46) an EXECUTABLE (mode 100755) or Git LFS pointer target is refused, never overwritten
    # (an update action without execute_filemode drops the bit; the pointer would become raw JSON)
    fake = FakeGitLab(_repo_files())
    lfs = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"ab" * 32 + b"\nsize 12\n"
    fake.commit_to("main", {"locales/fr-FR.json": b'{"x":"exec"}', "locales/de-DE.json": lfs})
    fake.modes["locales/fr-FR.json"] = "100755"
    c = _conn("execlfs", {"target_locales": "fr-FR, de-DE, es-ES"})
    ok, msg, res = _run(c, fake)
    errs = " ".join(f["error"] for f in res["failures"])
    for want in (
        "locales/fr-FR.json is a file marked executable",
        "locales/de-DE.json is a Git LFS file",
    ):
        assert want in errs, (want, errs)
    files = fake.files_at(f"loc/{c.id}")
    assert files["locales/fr-FR.json"] == b'{"x":"exec"}', "the executable file was overwritten"
    assert files["locales/de-DE.json"] == lfs, "the LFS pointer was replaced by raw content"
    assert "locales/es-ES.json" in files, "the healthy locale still lands"
    print("  I: an executable or Git LFS pointer target is refused per pair, others still land")
    # the LFS content check costs a read only for a pair that would be written (an unchanged
    # pair is skipped first), and every read goes to the pinned project id
    fake.log.clear()
    ok, msg, res = _run(c, fake)
    reads = [p for m, p, b in fake.log if m == "GET" and p.startswith("/repository/files/")]
    assert not any("es-ES" in p for p in reads), ("unchanged pair read", reads)
    assert any("de-DE" in p for p in reads), ("refused pair is re-checked", reads)
    enc = quote(REPO, safe="")
    assert all(p == f"/projects/{enc}" or not p.startswith("/projects/") for m, p, b in fake.log), (
        fake.log
    )
    assert fake.files_at(f"loc/{c.id}")["locales/de-DE.json"] == lfs, "pointer kept on rerun"
    print("  I: unchanged pairs are not read for the LFS check; reads stay on the pinned project")


if __name__ == "__main__":
    main()
