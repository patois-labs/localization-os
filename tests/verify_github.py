"""GitHub connector + shared git-provider core: fully offline via httpx.MockTransport.

FakeGitHub models the parts of GitHub's REST v3 the connector relies on, with the platform's
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

ASCII-only prints; final line is 'verify_github OK'.
"""

# ruff: noqa: E501, UP031
import base64
import hashlib
import json
import logging
import os
import re
import sys
from urllib.parse import parse_qs, unquote, urlsplit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_github_")

import httpx
from sqlmodel import Session, select

from app import config as app_config
from app.db import engine, init_db
from app.models import Connection, Segment, SegStatus, SyncLog
from app.services import key_files, translate
from app.services.connectors import store as cstore
from app.services.connectors.github import GitHubConnector

TOKEN = "ghp_FAKE_TOKEN_for_tests_0123456789"  # pregate: allow-secret offline fixture
REPO = "acme/site"


def _sha(*parts) -> str:
    h = hashlib.sha1()  # noqa: S324 (fake git object ids, not security)
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class FakeGitHub:
    """An in-memory GitHub repository behind httpx.MockTransport."""

    def __init__(self, files=None, tree_limit=None, token=TOKEN, push=True):
        self.owner, self.name = REPO.split("/")
        self.token = token
        self.push = push
        self.default_branch = "main"
        self.tree_limit = tree_limit
        self.blobs: dict = {}
        self.trees: dict = {}  # tree sha -> {path: blob sha}
        self.commits: dict = {}  # commit sha -> {tree, parents, message}
        self.refs: dict = {}  # branch -> commit sha
        self.prs: list = []
        self.log: list = []  # (method, path, body)
        self.n_commits = 0
        self.before_patch = None  # hook(self, branch) run just before a ref PATCH
        self.noise_prs = 0
        self.dirbase: dict = {}
        self.pr_pages: list = []
        self.blob_error = False
        self.big_file_paths: set = set()
        self.full_name = REPO  # what GET /repos answers (a redirect to another repo changes it)
        self.archived = False
        self.pr_fail = False  # POST /pulls answers 500 (the commit already landed)
        self.protected: set = set()  # branches the host reports as protected
        self.modes: dict = {}  # path -> git mode other than 100644 (120000 symlink, 160000 submodule)
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
        """Test helper: merge the PR (fast-forward the base to the PR head)."""
        pr = next(p for p in self.prs if p["number"] == number)
        self.refs[pr["base"]] = self.refs[pr["head"]]
        pr["state"], pr["merged"] = "closed", True

    # ---------------------------------------------------------------- the API
    def _handle(self, req: httpx.Request) -> httpx.Response:
        url = urlsplit(str(req.url))
        path = unquote(url.path)
        if path.startswith("/api/v3"):
            path = path[len("/api/v3") :]
        body = json.loads(req.content) if req.content else None
        self.log.append((req.method, path, body))
        auth = req.headers.get("authorization", "")
        if auth != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": f"Bad credentials {auth}"})
        for hdr, val in req.headers.items():
            if hdr.lower() != "authorization" and self.token in val:
                return httpx.Response(400, json={"message": "token leaked in a header"})
        if self.token in str(req.url):
            return httpx.Response(400, json={"message": "token leaked in the url"})
        prefix = f"/repos/{self.owner}/{self.name}"
        if not path.startswith(prefix):
            return httpx.Response(404, json={"message": "Not Found"})
        rest = path[len(prefix) :]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        m = req.method
        if rest == "" and m == "GET":
            return httpx.Response(
                200,
                json={
                    "default_branch": self.default_branch,
                    "full_name": self.full_name,
                    "archived": self.archived,
                    "permissions": {"push": self.push},
                },
            )
        mm = re.match(r"^/branches/(.+)$", rest)
        if mm and m == "GET":
            b = mm.group(1)
            if b not in self.refs:
                return httpx.Response(404, json={"message": "Branch not found"})
            return httpx.Response(200, json={"name": b, "protected": b in self.protected})
        if not self.push and m != "GET":
            return httpx.Response(
                403, json={"message": "Resource not accessible by personal access token"}
            )
        mm = re.match(r"^/git/ref/heads/(.+)$", rest)
        if mm and m == "GET":
            b = mm.group(1)
            if b not in self.refs:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200, json={"ref": f"refs/heads/{b}", "object": {"sha": self.refs[b]}}
            )
        if rest == "/git/refs" and m == "POST":
            ref = (body or {}).get("ref", "")
            b = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else None
            if b is None or ".." in b:
                return httpx.Response(422, json={"message": "Reference name is invalid"})
            if b in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            if body.get("sha") not in self.commits:
                return httpx.Response(422, json={"message": "Object does not exist"})
            self.refs[b] = body["sha"]
            return httpx.Response(201, json={"ref": ref, "object": {"sha": body["sha"]}})
        mm = re.match(r"^/git/refs/heads/(.+)$", rest)
        if mm and m == "PATCH":
            b = mm.group(1)
            if self.before_patch:
                self.before_patch(self, b)
            if b not in self.refs:
                return httpx.Response(404, json={"message": "Not Found"})
            new = (body or {}).get("sha")
            if new not in self.commits:
                return httpx.Response(422, json={"message": "Object does not exist"})
            if not body.get("force") and self.refs[b] not in self._ancestors(new):
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
            self.refs[b] = new
            return httpx.Response(200, json={"ref": f"refs/heads/{b}", "object": {"sha": new}})
        mm = re.match(r"^/git/commits/([0-9a-f]+)$", rest)
        if mm and m == "GET":
            c = self.commits.get(mm.group(1))
            if not c:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"sha": mm.group(1), "tree": {"sha": c["tree"]}})
        if rest == "/git/blobs" and m == "POST" and self.blob_error:
            return httpx.Response(422, json={"message": f"rejected content for token {self.token}"})
        if rest == "/git/blobs" and m == "POST":
            data = base64.b64decode(body["content"])
            return httpx.Response(201, json={"sha": self._blob(data)})
        mm = re.match(r"^/git/blobs/([0-9a-f]+)$", rest)
        if mm and m == "GET":
            b = self.blobs.get(mm.group(1))
            if b is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200, json={"content": base64.b64encode(b).decode(), "encoding": "base64"}
            )
        if rest == "/git/trees" and m == "POST":
            base = self.trees.get(body.get("base_tree"))
            if base is None:
                return httpx.Response(422, json={"message": "base_tree does not exist"})
            flat = dict(base)
            for e in body.get("tree") or []:
                if e.get("sha") not in self.blobs:
                    return httpx.Response(422, json={"message": "tree.sha does not exist"})
                if ".." in e["path"].split("/") or e["path"].startswith("/"):
                    return httpx.Response(422, json={"message": "tree.path is invalid"})
                flat[e["path"]] = e["sha"]
            tsha = _sha("tree", json.dumps(flat, sort_keys=True))
            self.trees[tsha] = flat
            return httpx.Response(201, json={"sha": tsha})
        if rest == "/git/commits" and m == "POST":
            if body["tree"] not in self.trees or any(
                p not in self.commits for p in body["parents"]
            ):
                return httpx.Response(422, json={"message": "tree or parent does not exist"})
            self.n_commits += 1
            csha = _sha(
                "commit", body["tree"], ",".join(body["parents"]), body["message"], self.n_commits
            )
            self.commits[csha] = {
                "tree": body["tree"],
                "parents": body["parents"],
                "message": body["message"],
            }
            return httpx.Response(201, json={"sha": csha})
        mm = re.match(r"^/git/trees/([0-9a-f]+)$", rest)
        if mm and m == "GET":
            return self._tree(mm.group(1), q.get("recursive"))
        mm = re.match(r"^/contents/(.+)$", rest)
        if mm and m == "GET":
            sha = self._resolve(q.get("ref") or self.default_branch)
            if sha is None:
                return httpx.Response(404, json={"message": "No commit found for the ref"})
            flat = self.trees[self.commits[sha]["tree"]]
            p = mm.group(1)
            if p not in flat:
                dirs = {x.rsplit("/", 1)[0] for x in flat if "/" in x}
                if p in dirs:
                    return httpx.Response(
                        200,
                        json=[{"type": "file", "path": x} for x in flat if x.startswith(p + "/")],
                    )
                return httpx.Response(404, json={"message": "Not Found"})
            data = self.blobs[flat[p]]
            # GitHub answers a symlink that points at a regular file with THAT file's content
            # (docs: repos/contents), so the contents call alone cannot see the link.
            if self.modes.get(p) == "160000":
                return httpx.Response(200, json={"type": "submodule", "path": p, "sha": flat[p]})
            if p in self.big_file_paths:
                return httpx.Response(
                    200,
                    json={
                        "type": "file",
                        "sha": flat[p],
                        "size": len(data),
                        "content": "",
                        "encoding": "none",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "sha": flat[p],
                    "size": len(data),
                    "content": base64.b64encode(data).decode(),
                    "encoding": "base64",
                },
            )
        mm = re.match(r"^/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)$", rest)
        if mm and m == "GET":
            a, b = mm.group(1), mm.group(2)
            if a not in self.commits or b not in self.commits:
                return httpx.Response(404, json={"message": "Not Found"})
            if a == b:
                st = "identical"
            elif a in self._ancestors(b):
                st = "ahead"
            elif b in self._ancestors(a):
                st = "behind"
            else:
                st = "diverged"
            return httpx.Response(200, json={"status": st})
        if rest == "/pulls" and m == "GET":
            head = q.get("head", "")
            if ":" not in head:
                return httpx.Response(422, json={"message": "head must be user:branch"})
            hb = head.split(":", 1)[1]
            real = [
                self._pr_json(p)
                for p in self.prs
                if p["state"] == q.get("state", "open")
                and p["head"] == hb
                and (not q.get("base") or p["base"] == q["base"])
            ]
            # a server that ignored the head filter would page through unrelated PRs first
            noise = [
                self._pr_json(
                    {
                        "number": 1000 + i,
                        "head": f"noise-{i}",
                        "base": "main",
                        "state": "open",
                        "title": "x",
                        "merged": False,
                    }
                )
                for i in range(self.noise_prs)
            ]
            rows = noise + real
            per, page = int(q.get("per_page", 30)), int(q.get("page", 1))
            self.pr_pages.append(page)
            return httpx.Response(200, json=rows[(page - 1) * per : page * per])
        if rest == "/pulls" and m == "POST" and self.pr_fail:
            return httpx.Response(500, json={"message": "Server Error"})
        if rest == "/pulls" and m == "POST":
            h, b = body["head"], body["base"]
            if h not in self.refs or b not in self.refs:
                return httpx.Response(422, json={"message": "head or base does not exist"})
            if any(p["state"] == "open" and p["head"] == h and p["base"] == b for p in self.prs):
                return httpx.Response(
                    422, json={"message": f"A pull request already exists for {self.owner}:{h}."}
                )
            if self.refs[h] in self._ancestors(self.refs[b]):
                return httpx.Response(422, json={"message": "No commits between base and head"})
            pr = {
                "number": len(self.prs) + 1,
                "head": h,
                "base": b,
                "state": "open",
                "merged": False,
                "title": body["title"],
                "body": body.get("body", ""),
            }
            self.prs.append(pr)
            return httpx.Response(201, json=self._pr_json(pr))
        mm = re.match(r"^/pulls/(\d+)$", rest)
        if mm:
            pr = next((p for p in self.prs if p["number"] == int(mm.group(1))), None)
            if pr is None:
                return httpx.Response(404, json={"message": "Not Found"})
            if m == "PATCH":
                for k in ("title", "body"):
                    if k in body:
                        pr[k] = body[k]
                if body.get("state") == "closed" and pr["state"] == "open":
                    pr["state"] = "closed"
            return httpx.Response(200, json=self._pr_json(pr))
        return httpx.Response(404, json={"message": f"unexpected {m} {rest}"})

    def _pr_json(self, p: dict) -> dict:
        return {
            "number": p["number"],
            "state": p["state"],
            "merged": p.get("merged", False),
            "title": p["title"],
            "body": p.get("body", ""),
            "html_url": f"https://github.test/{REPO}/pull/{p['number']}",
            "head": {"ref": p["head"], "repo": {"full_name": REPO}},
            "base": {"ref": p["base"]},
        }

    def _tree(self, tsha: str, recursive) -> httpx.Response:
        flat = self.trees.get(tsha)
        if flat is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if recursive:
            entries = []
            dirs = set()
            for p in sorted(flat):
                parts = p.split("/")
                for i in range(1, len(parts)):
                    dirs.add("/".join(parts[:i]))
                entries.append(
                    {
                        "path": p,
                        "mode": self.modes.get(p, "100644"),
                        "type": "commit" if self.modes.get(p) == "160000" else "blob",
                        "sha": flat[p],
                        "size": len(self.blobs[flat[p]]),
                    }
                )
            entries += [
                {"path": d, "mode": "040000", "type": "tree", "sha": _sha("dir", tsha, d)}
                for d in sorted(dirs)
            ]
            if self.tree_limit and len(entries) > self.tree_limit:
                return httpx.Response(
                    200, json={"sha": tsha, "tree": entries[: self.tree_limit], "truncated": True}
                )
            return httpx.Response(200, json={"sha": tsha, "tree": entries, "truncated": False})
        # one level: the sha of a sub-directory tree is derived so it can be looked up again
        return httpx.Response(
            200,
            json={
                "sha": tsha,
                "tree": self._level(tsha, flat, self.dirbase.get(tsha, "")),
                "truncated": False,
            },
        )

    def _level(self, tsha, flat, base):
        out, seen = [], set()
        for p in sorted(flat):
            if base and not p.startswith(base + "/"):
                continue
            rel = p[len(base) + 1 :] if base else p
            head = rel.split("/")[0]
            if "/" in rel:
                if head not in seen:
                    seen.add(head)
                    sub = f"{base}/{head}" if base else head
                    self.dirbase[_sha("dir", tsha, sub)] = sub
                    self.trees.setdefault(
                        _sha("dir", tsha, sub),
                        {k: v for k, v in flat.items() if k.startswith(sub + "/")},
                    )
                    out.append(
                        {
                            "path": head,
                            "mode": "040000",
                            "type": "tree",
                            "sha": _sha("dir", tsha, sub),
                        }
                    )
            else:
                out.append(
                    {
                        "path": head,
                        "mode": self.modes.get(p, "100644"),
                        "type": "commit" if self.modes.get(p) == "160000" else "blob",
                        "sha": flat[p],
                        "size": len(self.blobs[flat[p]]),
                    }
                )
        return out


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
        "base_url": "https://github.test/api/v3",
        "base_branch": "main",
        "source_paths": "locales/{locale}.json, po/{locale}.po",
        "source_locale": "en-US",
        "target_locales": "fr-FR, de-DE",
    }
    cfg.update(cfg_extra or {})
    return cstore.create("github", name, cfg, secret or {"token": TOKEN}, actor="admin")


def _run(conn, fake, **kw):
    kw.setdefault("actor", "auto")
    return cstore.github_translate(conn.id, transport=fake.transport(), **kw)


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
    assert GitHubConnector.capability.topology == "T3"

    # ---- (1) provider basics: test(), default branch, create_branch 422 on an existing one
    fake = FakeGitHub(_repo_files())
    cx = GitHubConnector(
        {
            "repo": REPO,
            "base_url": "https://github.test/api/v3",
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
    bad = GitHubConnector(
        {
            "repo": REPO,
            "base_url": "https://github.test/api/v3",
            "source_paths": "locales/{locale}.json",
        },
        {"token": "wrong-token-123456"},
        transport=fake.transport(),
    )
    bad.connection_id = 1
    ok, msg = bad.test()
    assert not ok and "401" in msg and "wrong-token-123456" not in msg, msg
    ro = FakeGitHub(_repo_files(), push=False)
    cro = GitHubConnector(
        {
            "repo": REPO,
            "base_url": "https://github.test/api/v3",
            "source_paths": "locales/{locale}.json",
        },
        {"token": TOKEN},
        transport=ro.transport(),
    )
    cro.connection_id = 1
    ok, msg = cro.test()
    assert not ok and "cannot push" in msg, msg
    print(
        "  provider: test(), 401 scrubbed, read-only token reported, create_branch tolerates an existing branch (422)"
    )

    # ---- (2) first run: JSON nested + PO + plurals, ONE commit, ONE pull request
    fake = FakeGitHub(_repo_files())
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
    hashes = _cfg(c.id).get("github_hashes") or {}
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
    en3 = dict(EN_JSON, hello="Hello a third time")
    fake.commit_to("main", {"locales/en-US.json": _json(en3)}, "edit source again")
    ok, msg, _res4 = _run(c, fake)
    assert ok, msg
    assert len(fake.prs) == 2 and fake.prs[1]["state"] == "open", fake.prs
    assert all(w[1] != "/repos/acme/site/git/refs/heads/main" for w in fake.writes()), "never main"
    print("  merged PR -> branch fast-forwarded (never forced), a fresh PR is opened")

    # ---- (6) H: the base branch is never written, on the wire
    for m, p, b in fake.writes():
        assert not p.endswith("/heads/main"), (m, p)
        if p.endswith("/git/refs"):
            assert b["ref"] != "refs/heads/main", b
        if b and "force" in b:
            assert b["force"] is False, (m, p, b)
    print("  H: no wire request ever targeted main; every ref update is force=false")

    _more(cap)
    _review(cap)
    _stale_hash_666()
    print("verify_github OK")


def _stale_hash_666() -> None:
    """#666 (git_core, shared by GitHub/GitLab/Bitbucket): a file committed with a held key
    (kept at its old value) drops the pair's OLDER stored hash, so a source edit back to
    exactly that older text re-exports the file instead of being skipped as done."""
    fake = FakeGitHub(_repo_files())
    c = _conn("stale666", {"target_locales": "fr-FR"})
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"], (msg, res)
    br = f"loc/{c.id}"
    key = "locales/en-US.json:fr-FR"
    h1 = (_cfg(c.id).get("github_hashes") or {}).get(key)
    v1 = json.loads(fake.files_at(br)["locales/fr-FR.json"])
    assert h1 and v1["hello"], (h1, v1)
    # v2: the title and hello change; hello is held -> the file is committed with the new
    # title and the OLD hello (a mixed file)
    v2_src = dict(EN_JSON, hello="Hello once more", app={**EN_JSON["app"], "title": "Welcome back"})
    fake.commit_to("main", {"locales/en-US.json": _json(v2_src)})
    restore = _patch_pretranslate({"Hello once more"})
    try:
        ok, msg, res = _run(c, fake)
    finally:
        restore()
    assert ok and res["commit"] and res["held_back"] == 1, (msg, res)
    v2 = json.loads(fake.files_at(br)["locales/fr-FR.json"])
    assert v2["app"]["title"] != v1["app"]["title"] and v2["hello"] == v1["hello"], (v1, v2)
    assert key not in (_cfg(c.id).get("github_hashes") or {}), (
        "#666: the stale v1 hash must be dropped",
        _cfg(c.id).get("github_hashes"),
    )
    # the source goes back to exactly v1: the file must be re-exported (before #666 the v1
    # hash matched and the mixed file was skipped as done)
    fake.commit_to("main", {"locales/en-US.json": _json(EN_JSON)})
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"], (msg, res)
    assert "locales/fr-FR.json" in [f["path"] for f in res["files"]], res
    v3 = json.loads(fake.files_at(br)["locales/fr-FR.json"])
    assert v3["app"]["title"] == v1["app"]["title"], (v1, v3)
    assert (_cfg(c.id).get("github_hashes") or {}).get(key) == h1
    ok, msg, res = _run(c, fake)
    assert ok and not res["commit"] and res["skipped"] >= 1, ("then suppressed", msg, res)
    print("  #666: a file committed with a held key drops its stale hash; a revert re-exports")


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
    fake = FakeGitHub(_repo_files())
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
        hashes = _cfg(c.id).get("github_hashes") or {}
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
    hashes = _cfg(c.id).get("github_hashes") or {}
    assert "locales/en-US.json:fr-FR" in hashes, hashes
    assert "locales/en-US.json:de-DE" not in hashes, hashes
    _set_row("Hello there", "de-DE", status=SegStatus.APPROVED)
    ok, msg, res = _run(c, fake)
    assert ok and "hello" in json.loads(fake.files_at(f"loc/{c.id}")["locales/de-DE.json"]), msg
    assert "locales/en-US.json:de-DE" in (_cfg(c.id).get("github_hashes") or {})
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
    fake = FakeGitHub({**_repo_files(), "locales/fr-FR.json": _json(existing)})
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
    fake = FakeGitHub(_repo_files())
    c = _conn("allheld", {"target_locales": "fr-FR"})
    restore = _patch_pretranslate(None, blank=True)
    try:
        ok, msg, res = _run(c, fake)
    finally:
        restore()
    assert not ok and not fake.writes() and not _cfg(c.id).get("github_hashes"), (msg, res)
    assert f"loc/{c.id}" not in fake.refs, "no branch for an empty run"
    assert _status(c.id)[0] == "error"
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"], (msg, res)
    print("  F: nothing translated -> no branch, no commit, no hash; the healthy re-run completes")

    # ---- (10) A: hostile article ids are refused before any request
    fake = FakeGitHub(_repo_files())
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
        {"base_url": "http://169.254.169.254"},
        {"base_url": "http://localhost:8080"},
        {"base_url": "http://127.0.0.1"},
        {"base_url": "http://github.example.com/api/v3"},
        # the token must reach only the host the operator SEES (shared git_core.validate_api_url):
        # userinfo, query, fragment, backslash and dot segments are refused before any request
        {"base_url": "https://github.test@evil.example/api/v3"},
        {"base_url": "https://user:pw@github.test/api/v3"},
        {"base_url": "https://github.test/api/v3?x=1"},
        {"base_url": "https://github.test/api/v3#frag"},
        {"base_url": r"https://github.test\@evil.example/api/v3"},
        {"base_url": "https://github.test/api/../v3"},
        {"base_url": "https://github.test/api/%2e%2e/v3"},
        {"base_url": "https://github.test/api v3"},
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
    fake = FakeGitHub(_repo_files())
    cx = _conn("tobase", {"branch_pattern": "main", "target_locales": "fr-FR"})
    ok, msg, _r = _run(cx, fake)
    assert not ok and not fake.writes() and "base" in msg.lower(), msg
    fake2 = FakeGitHub(_repo_files())
    cx = _conn("todefault", {"branch_pattern": "release/{connection}", "target_locales": "fr-FR"})
    fake2.default_branch = f"release/{cx.id}"
    fake2.refs[fake2.default_branch] = fake2.refs["main"]
    ok, msg, _r = _run(cx, fake2)
    assert not ok and not fake2.writes() and "default" in msg.lower(), msg
    print("  H: a working branch equal to the base or the repository default branch is refused")

    # ---- (13) G: a 422 on a branch that is not a fast-forward is loud, never an overwrite
    fake = FakeGitHub(_repo_files())
    c = _conn("race", {"target_locales": "fr-FR"})

    def stray(f, b):
        f.before_patch = None
        f.commit_to(b, {"stray.txt": b"someone else pushed"}, "stray")

    fake.before_patch = stray
    ok, msg, res = _run(c, fake)
    assert not ok and "moved" in msg, msg
    assert fake.files_at(f"loc/{c.id}").get("stray.txt") == b"someone else pushed"
    assert "locales/fr-FR.json" not in fake.files_at(f"loc/{c.id}")
    assert not _cfg(c.id).get("github_hashes"), "a failed push records no hash"
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
    fake = FakeGitHub({**_repo_files(), **many}, tree_limit=5)
    cx = GitHubConnector(
        {
            "repo": REPO,
            "base_url": "https://github.test/api/v3",
            "source_paths": "locales/{locale}.json",
        },
        {"token": TOKEN},
        transport=fake.transport(),
    )
    cx.connection_id = 1
    prov = cx.provider()
    got = {f.path for f in prov.list_files(fake.refs["main"], "locales") if f.kind == "file"}
    want = {p for p in fake.files_at("main") if p.startswith("locales/")}
    assert got == want, (len(got), len(want))
    n_tree = len([1 for m, p, b in fake.log if m == "GET" and "/git/trees/" in p])
    assert n_tree > 3, ("truncated listing walked directory by directory", n_tree)
    c = _conn("trunc", {"target_locales": "fr-FR"})
    ok, msg, res = _run(c, fake)
    assert ok and res["created"] == 2, (msg, res)
    fake = FakeGitHub(_repo_files())
    fake.noise_prs = 150
    c = _conn("noise", {"target_locales": "fr-FR"})
    ok, _m, _r = _run(c, fake)
    assert ok and len(fake.prs) == 1
    fake.commit_to("main", {"locales/en-US.json": _json(dict(EN_JSON, hello="Another"))})
    ok, _m, _r = _run(c, fake)
    assert ok and len(fake.prs) == 1 and 2 in fake.pr_pages, ("PR found on page 2", fake.pr_pages)
    print("  pagination: truncated trees listed dir by dir; the open PR is found past page 1")

    # ---- (15) H: WriteBudget ceiling (one unit per file); the rest stays pending
    fake = FakeGitHub(_repo_files())
    c = _conn("budget", {"target_locales": "fr-FR, de-DE"})
    saved = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        ok, msg, res = _run(c, fake)
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    assert res["ceiling_hit"] and res["created"] == 1 and len(res["failures"]) == 3, res
    assert len(_cfg(c.id).get("github_hashes") or {}) == 1
    ok, msg, res = _run(c, fake)
    assert ok and res["created"] == 3 and not res["ceiling_hit"], (msg, res)
    print("  H: write ceiling stops after 1 file; the next run completes the other 3")

    # ---- (16) H: mock MT + real egress is refused (no external writes)
    fake = FakeGitHub(_repo_files())
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
    fake = FakeGitHub(_repo_files())
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
    c = cstore.create("github", "pin", dict(base), {"token": TOKEN}, actor="a")
    cstore.update(c.id, "pin", {"base_url": "https://api.github.com"}, None, actor="a")
    assert dec_secret(cstore.get(c.id).secret_enc).get("token") == TOKEN, "explicit default host"
    cstore.update(c.id, "pin", {"base_branch": "dev"}, None, actor="a")
    assert dec_secret(cstore.get(c.id).secret_enc).get("token") == TOKEN
    cstore.update(c.id, "pin", {"repo": "acme/other"}, None, actor="a")
    assert not dec_secret(cstore.get(c.id).secret_enc).get("token"), "repo change clears the token"
    c = cstore.create("github", "pin2", dict(base), {"token": TOKEN}, actor="a")
    cstore.update(c.id, "pin2", {"base_url": "https://ghe.evil.example/api/v3"}, None, actor="a")
    assert not dec_secret(cstore.get(c.id).secret_enc).get("token"), "host change clears the token"
    print("  H: repository / API host change clears the stored token; same-host edits keep it")

    # ---- (19) locale styles + removed keys + retry bypass + missing source
    fake = FakeGitHub({"i18n/en.json": _json({"a": "One", "b": "Two"})})
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
    fake = FakeGitHub(_repo_files())
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

    assert "github" in REGISTRY
    tc = TestClient(app)
    assert "GitHub" in tc.get("/connectors").text
    typ = tc.get("/connectors/github").text
    assert "Repository" in typ and "Personal access token" in typ, "type form renders"
    from app.cli.__main__ import build_parser

    assert "connector github-translate" in _leaf_paths(build_parser())
    assert "/api/connectors/{cid}/github/translate" in {
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
    fake = FakeGitHub(_repo_files())
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
    assert "locales/en-US.json:fr-FR" not in (_cfg(c.id).get("github_hashes") or {}), "no hash"
    fake = FakeGitHub(_repo_files())
    fake.modes["locales/en-US.json"] = "120000"
    c = _conn("srclink", {"target_locales": "fr-FR", "source_paths": "locales/{locale}.json"})
    ok, msg, res = _run(c, fake)
    assert not fake.writes() and any("symlink" in f["error"] for f in res["failures"]), (msg, res)
    print(
        "  A: a symlink at a target path is not replaced; a symlinked source is refused, no writes"
    )

    # ---- (24) a protected working branch is refused before any write
    fake = FakeGitHub(_repo_files())
    c = _conn("prot", {"target_locales": "fr-FR"})
    fake.refs[f"loc/{c.id}"] = fake.refs["main"]
    fake.protected.add(f"loc/{c.id}")
    ok, msg, _r = _run(c, fake)
    assert not ok and "protected" in msg and not fake.writes(), msg
    print("  H: a protected working branch is refused, nothing written")

    # ---- (25) repository identity pinned in the answer; archived repo refused
    fake = FakeGitHub(_repo_files())
    fake.full_name = "evil/other"
    c = _conn("ident", {"target_locales": "fr-FR"})
    ok, msg, _r = _run(c, fake)
    assert not ok and "evil/other" in msg and not fake.writes(), msg
    fake = FakeGitHub(_repo_files())
    fake.archived = True
    ok, msg, _r = _run(_conn("arch", {"target_locales": "fr-FR"}), fake)
    assert not ok and "archived" in msg and not fake.writes(), msg
    print("  H: an answer for a different repository, or an archived one, is refused")

    # ---- (26) a malformed token is refused without being echoed or sent
    fake = FakeGitHub(_repo_files())
    weird = "ghp_SECRETPART one\ntwo_0123456789"  # pregate: allow-secret offline fixture
    c = _conn("badtok", {"target_locales": "fr-FR"}, {"token": weird})
    ok, msg, res = _run(c, fake)
    assert not ok and not fake.log and "SECRETPART" not in msg, msg
    assert not any("SECRETPART" in line for line in cap.lines), "token fragment in a log"
    print("  H: a token with spaces or line breaks is refused, never echoed, never sent")

    # ---- (27) commit landed but the PR open failed: the next run opens the PR, no new commit
    fake = FakeGitHub(_repo_files())
    c = _conn("orphan", {"target_locales": "fr-FR"})
    fake.pr_fail = True
    ok, msg, res = _run(c, fake)
    br = f"loc/{c.id}"
    assert not ok and br in fake.refs and not fake.prs, (msg, fake.prs)
    assert not _cfg(c.id).get("github_hashes"), "no hash when the PR was not opened"
    fake.pr_fail = False
    head = fake.refs[br]
    ok, msg, res = _run(c, fake)
    assert ok and len(fake.prs) == 1 and fake.refs[br] == head, (msg, fake.prs)
    assert res["pull_request"]["number"] == 1 and not res["commit"], res
    assert len(_cfg(c.id).get("github_hashes") or {}) == 2, _cfg(c.id)
    n = len(fake.writes())
    ok, msg, res = _run(c, fake)
    assert ok and res["skipped"] == 2 and len(fake.writes()) == n, "steady state afterwards"
    print("  E: an orphaned working branch gets its pull request on the next run (no new commit)")

    # ---- (28) workflow_id: the workflow path replaces the direct pretranslate and reports
    fake = FakeGitHub(_repo_files())
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
    fake = FakeGitHub(_repo_files())
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
        {"github_branch_state": {"branch": br, "head": human, "base": human, "gen": 1}},
        None,
    )
    assert "github_branch_state" not in _cfg(c.id), _cfg(c.id)
    ok, msg, _r = _run(c, fake)
    assert not ok and "not created by this connection" in msg and not fake.writes(), msg
    print(
        "  H: a same-named branch this connection did not create is refused; forged record dropped"
    )

    # ---- (30) reviewer edits to the PR title/body survive; base moved without touching
    # our files keeps the same branch and PR
    fake = FakeGitHub(_repo_files())
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
    assert not any(w[2] and "title" in w[2] for w in fake.writes() if "/pulls/" in w[1]), (
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
    fake.merge_pr(2)
    edit_source(fake, "Hello, after merge")
    ok, msg, res = _run(c, fake)
    assert ok and res["commit"] and f"loc/{c.id}-3" in fake.refs, (msg, sorted(fake.refs))
    assert fake.refs[br2] == reviewer and fake.prs[-1]["head"] == f"loc/{c.id}-3", fake.prs
    print("  reviewer commits on an open PR are never buried; after the merge a fresh branch")

    # ---- (33) PR closed without merging (or squash-merged), base moved: a new branch from
    # the base, the old branch untouched, the closed PR not reopened
    fake = FakeGitHub(_repo_files())
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

    # ---- (34) an EXECUTABLE (mode 100755) or Git LFS pointer target is refused, never overwritten
    # (our commit writes mode 100644 and raw JSON); other locales still land
    fake = FakeGitHub(_repo_files())
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
    assert "locales/fr-FR.json:fr-FR" not in " ".join(_cfg(c.id).get("github_hashes") or {}), "hash"
    print("  A: an executable or Git LFS pointer target is refused per pair, others still land")

    # ---- (35) LFS detection matches git-lfs (whitespace trimmed, CRLF, legacy version values)
    # and never fires on a real locale file; an executable SOURCE is read and translated
    from app.services.connectors import git_core

    body = "oid sha256:" + "ab" * 32 + "\nsize 12\n"
    for v in (
        "https://git-lfs.github.com/spec/v1",
        "https://hawser.github.com/spec/v1",
        "http://git-media.io/v/2",
    ):
        for pad in ("", "\n", "  \r\n", "﻿"):
            ptr = (pad + f"version {v}\n" + body).encode()
            assert git_core.is_lfs_pointer(ptr), (v, pad)
            assert git_core.is_lfs_pointer(ptr.replace(b"\n", b"\r\n")), (v, pad, "crlf")
    for not_ptr in (
        b'{"version": "https://git-lfs.github.com/spec/v1"}',
        b'{\n  "version https://git-lfs.github.com/spec/v1": "x"\n}',
        b'msgid ""\nmsgstr "version https://git-lfs.github.com/spec/v1"\n',
        b"version https://example.com/spec/v1\n",
        b"version https://git-lfs.github.com/spec/v1" + b" " * 1024,
    ):
        assert not git_core.is_lfs_pointer(not_ptr), not_ptr
    fake = FakeGitHub(_repo_files())
    padded = b"\r\n" + lfs.replace(b"\n", b"\r\n")
    fake.commit_to("main", {"locales/de-DE.json": padded})
    fake.modes["locales/en-US.json"] = "100755"
    c = _conn("execsrc", {"target_locales": "fr-FR, de-DE"})
    ok, msg, res = _run(c, fake)
    errs = " ".join(f["error"] for f in res["failures"])
    assert "locales/de-DE.json is a Git LFS file" in errs, errs
    assert "en-US" not in errs, errs
    files = fake.files_at(f"loc/{c.id}")
    assert files["locales/de-DE.json"] == padded, "a padded CRLF LFS pointer was overwritten"
    assert "locales/fr-FR.json" in files, "an executable source is still translated"
    print(
        "  A: padded/CRLF/legacy LFS pointers refused; JSON/PO never match; executable source read"
    )


if __name__ == "__main__":
    main()
