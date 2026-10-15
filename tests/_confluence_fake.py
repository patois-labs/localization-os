"""A hermetic fake Confluence Cloud tenant (REST v2 + the v1 search endpoint) for the offline
connector suites. Served through httpx.MockTransport.

It models the real system where the connector's design depends on it (blueprint taxonomy G):
  * page titles are UNIQUE per space (a duplicate create or rename answers 400),
  * PUT /pages/{id} requires version.number == current + 1 (anything else answers 409),
  * POST /pages requires a numeric spaceId of an existing space and an existing parentId,
  * v2 identifies spaces by id, so a space KEY is resolved through GET /spaces?keys=,
  * list endpoints paginate with an opaque cursor in `_links.next` (the connector must follow
    only the cursor),
  * v2 has NO CQL: search is the v1 endpoint and answers ids only.
Both the direct site paths (/wiki/api/v2, /wiki/rest/api) and the OAuth gateway paths
(/ex/confluence/{cloudid}/wiki/api/v2, /ex/confluence/{cloudid}/rest/api) are served; the
`log` records (method, path-after-prefix, query, body, authorization-kind).
"""

import json
import re

import httpx


class FakeConfluence:
    def __init__(self, *, page_size: int = 100):
        self.spaces: dict[str, dict] = {}
        self.pages: dict[str, dict] = {}
        self._next = 1000
        self.page_size = page_size
        self.log: list[dict] = []
        self.cql_seen: list[str] = []
        self.fail_put: int = 0  # answer this many PUTs with 500
        self.conflict_once: bool = False  # answer the next PUT with a 409 (a concurrent save)
        self.statuses: dict[str, int] = {}  # path -> forced status
        self.throttle: int = 0  # answer this many write requests with 429

    # --- seeding ---
    def add_space(self, key: str, name: str = "", sid: str | None = None) -> str:
        sid = sid or str(self._next)
        self._next += 1
        self.spaces[sid] = {"id": sid, "key": key, "name": name or key}
        return sid

    def add_page(
        self,
        space_key: str,
        title: str,
        body: str = "<p>x</p>",
        *,
        parent: str | None = None,
        labels=(),
        pid: str | None = None,
        status: str = "current",
    ) -> str:
        sid = next(k for k, v in self.spaces.items() if v["key"] == space_key)
        pid = pid or str(self._next)
        self._next += 1
        self.pages[pid] = {
            "id": pid,
            "title": title,
            "spaceId": sid,
            "parentId": parent,
            "status": status,
            "version": {"number": 1},
            "body": body,
            "labels": list(labels),
        }
        return pid

    def by_title(self, title: str):
        return [p for p in self.pages.values() if p["title"] == title and p["status"] == "current"]

    def children(self, pid: str):
        return [p for p in self.pages.values() if p["parentId"] == pid and p["status"] == "current"]

    def writes(self, method: str | None = None):
        return [
            e
            for e in self.log
            if e["method"] in ("POST", "PUT") and (not method or e["method"] == method)
        ]

    # --- wire ---
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    @staticmethod
    def _json(status, obj):
        return httpx.Response(status, json=obj)

    def _page_json(self, p, *, body=True, labels=False):
        out = {
            "id": p["id"],
            "status": p["status"],
            "title": p["title"],
            "spaceId": p["spaceId"],
            "parentId": p["parentId"],
            "version": dict(p["version"]),
        }
        if body:
            out["body"] = {"storage": {"representation": "storage", "value": p["body"]}}
        if labels:
            out["labels"] = {"results": [{"name": n} for n in p["labels"]]}
        return out

    def _paged(self, rows, req, path, **kw):
        q = req.url.params
        start = int(q.get("cursor") or 0)
        limit = min(int(q.get("limit") or self.page_size), self.page_size)
        chunk = rows[start : start + limit]
        out = {"results": [self._page_json(p, **kw) for p in chunk], "_links": {}}
        if start + limit < len(rows):
            out["_links"]["next"] = f"/wiki/api/v2{path}?cursor={start + limit}&limit={limit}"
        return self._json(200, out)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        m = re.match(r"^/ex/confluence/[^/]+(/.*)$", path)
        gateway = bool(m)
        rest = m.group(1) if m else path
        if rest.startswith("/wiki/api/v2"):
            rest = rest[len("/wiki/api/v2") :]
            api = "v2"
        elif rest.startswith("/wiki/rest/api") and not gateway:
            rest = rest[len("/wiki/rest/api") :]
            api = "v1"
        elif rest.startswith("/rest/api") and gateway:
            rest = rest[len("/rest/api") :]
            api = "v1"
        else:
            self.log.append({"method": req.method, "path": path, "api": "?", "q": {}, "body": None})
            return self._json(404, {"path": path})
        body = json.loads(req.content) if req.content else None
        auth = req.headers.get("authorization", "")
        self.log.append(
            {
                "method": req.method,
                "path": rest,
                "api": api,
                "q": dict(req.url.params),
                "body": body,
                "auth": auth.split(" ")[0].lower() if auth else "",
                "gateway": gateway,
                "raw": path,
            }
        )
        if self.throttle > 0 and req.method != "GET":
            self.throttle -= 1
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"})
        if rest in self.statuses:
            return self._json(self.statuses[rest], {"message": "forced"})
        if api == "v1":
            return self._v1(rest, req)
        return self._v2(rest, req, body)

    def _v1(self, rest, req):
        if rest == "/search":
            cql = req.url.params.get("cql", "")
            self.cql_seen.append(cql)
            ids = self._eval_cql(cql)
            return self._json(
                200,
                {
                    "results": [{"content": {"id": i, "type": "page"}} for i in ids],
                    "_links": {},
                },
            )
        return self._json(404, {"path": rest})

    def _eval_cql(self, cql):
        """Just enough CQL: `space = "KEY"` / `space = KEY` clauses ANDed, `label = 'x'`,
        `title ~ "x"`; OR and parentheses are evaluated as a real boolean over those atoms."""
        expr = cql
        atoms = []

        def atom(mt):
            atoms.append(mt.group(0))
            return f"__A{len(atoms) - 1}__"

        expr = re.sub(r"(space|label|title|type)\s*(=|~)\s*(\"[^\"]*\"|'[^']*'|\w+)", atom, expr)
        expr = re.sub(r"\bAND\b", " and ", expr, flags=re.I)
        expr = re.sub(r"\bOR\b", " or ", expr, flags=re.I)

        def ev(p):
            res = []
            for a in atoms:
                k, _op, v = re.match(r"(\w+)\s*(=|~)\s*(.*)", a).groups()
                v = v.strip("\"'")
                if k == "type":
                    ok = v == "page"
                elif k == "space":
                    ok = self.spaces[p["spaceId"]]["key"] == v
                elif k == "label":
                    ok = v in p["labels"]
                else:
                    ok = v.lower() in p["title"].lower()
                res.append(ok)
            e = expr
            for i, ok in enumerate(res):
                e = e.replace(f"__A{i}__", str(ok))
            return bool(eval(e, {"__builtins__": {}}, {"True": True, "False": False}))  # noqa: S307

        try:
            return [p["id"] for p in self.pages.values() if p["status"] == "current" and ev(p)]
        except Exception:
            return []

    def _v2(self, rest, req, body):
        meth = req.method
        q = req.url.params
        if rest == "/spaces" and meth == "GET":
            rows = list(self.spaces.values())
            if q.get("keys"):
                keys = q["keys"].split(",")
                rows = [s for s in rows if s["key"] in keys]
            return self._json(200, {"results": rows, "_links": {}})
        m = re.match(r"^/spaces/(\d+)$", rest)
        if m and meth == "GET":
            s = self.spaces.get(m.group(1))
            return self._json(200, s) if s else self._json(404, {})
        m = re.match(r"^/spaces/(\d+)/pages$", rest)
        if m and meth == "GET":
            rows = [
                p
                for p in self.pages.values()
                if p["spaceId"] == m.group(1) and p["status"] == "current"
            ]
            return self._paged(rows, req, rest)
        if rest == "/pages" and meth == "GET":
            rows = [p for p in self.pages.values() if p["status"] == "current"]
            if q.get("space-id"):
                rows = [p for p in rows if p["spaceId"] == q["space-id"]]
            if q.get("title"):
                rows = [p for p in rows if p["title"] == q["title"]]
            return self._paged(rows, req, rest)
        if rest == "/pages" and meth == "POST":
            return self._create(body)
        m = re.match(r"^/pages/(\d+)(/.*)?$", rest)
        if m:
            pid, sub = m.group(1), m.group(2) or ""
            p = self.pages.get(pid)
            if p is None:
                return self._json(404, {})
            if sub == "" and meth == "GET":
                if p["status"] != "current" and not q.get("status"):
                    # v2 returns pages of any status by id; the connector must check
                    pass
                return self._json(
                    200,
                    self._page_json(
                        p,
                        body=q.get("body-format") == "storage",
                        labels=q.get("include-labels") == "true",
                    ),
                )
            if sub == "" and meth == "PUT":
                return self._update(p, body)
            if sub == "/direct-children" and meth == "GET":
                return self._paged(self.children(pid), req, rest, body=False)
            if sub == "/ancestors" and meth == "GET":
                chain = []
                cur = p["parentId"]
                while cur and cur in self.pages:
                    chain.append({"id": cur, "type": "page"})
                    cur = self.pages[cur]["parentId"]
                return self._json(200, {"results": list(reversed(chain)), "_links": {}})
        return self._json(404, {"path": rest})

    def _create(self, body):
        body = body or {}
        unknown = set(body) - {"spaceId", "status", "title", "parentId", "body", "subtype"}
        if unknown:
            return self._json(400, {"errors": [{"title": f"unknown field {sorted(unknown)}"}]})
        if str(body.get("spaceId")) not in self.spaces:
            return self._json(400, {"errors": [{"title": "spaceId not found"}]})
        if not (body.get("title") or "").strip():
            return self._json(400, {"errors": [{"title": "title required"}]})
        if body.get("parentId") and str(body["parentId"]) not in self.pages:
            return self._json(400, {"errors": [{"title": "parent not found"}]})
        if (body.get("body") or {}).get("representation") not in (None, "storage"):
            return self._json(400, {"errors": [{"title": "bad representation"}]})
        dup = [
            p
            for p in self.pages.values()
            if p["spaceId"] == str(body["spaceId"])
            and p["title"] == body["title"]
            and p["status"] == "current"
        ]
        if dup:
            return self._json(
                400, {"errors": [{"title": "A page with this title already exists in this space"}]}
            )
        pid = str(self._next)
        self._next += 1
        self.pages[pid] = {
            "id": pid,
            "title": body["title"],
            "spaceId": str(body["spaceId"]),
            "parentId": str(body["parentId"]) if body.get("parentId") else None,
            "status": "current",
            "version": {"number": 1},
            "body": (body.get("body") or {}).get("value", ""),
            "labels": [],
        }
        return self._json(200, self._page_json(self.pages[pid]))

    def _update(self, p, body):
        body = body or {}
        if self.fail_put > 0:
            self.fail_put -= 1
            return self._json(500, {"message": "boom"})
        if set(body) - {
            "id",
            "status",
            "title",
            "body",
            "version",
            "spaceId",
            "parentId",
            "ownerId",
        }:
            return self._json(400, {"errors": [{"title": "unknown field"}]})
        for need in ("id", "status", "title", "body", "version"):
            if need not in body:
                return self._json(400, {"errors": [{"title": f"{need} required"}]})
        if self.conflict_once:
            self.conflict_once = False
            p["version"] = {"number": p["version"]["number"] + 1}  # someone else saved
            return self._json(409, {"message": "version conflict"})
        if int((body.get("version") or {}).get("number", 0)) != p["version"]["number"] + 1:
            return self._json(409, {"message": "version conflict"})
        clash = [
            x
            for x in self.pages.values()
            if x["id"] != p["id"]
            and x["spaceId"] == p["spaceId"]
            and x["title"] == body["title"]
            and x["status"] == "current"
        ]
        if clash:
            return self._json(400, {"errors": [{"title": "A page with this title already exists"}]})
        p["title"] = body["title"]
        p["body"] = (body.get("body") or {}).get("value", "")
        p["version"] = {"number": p["version"]["number"] + 1}
        return self._json(200, self._page_json(p))
