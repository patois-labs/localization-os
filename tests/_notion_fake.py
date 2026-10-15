"""A stateful fake Notion workspace for the 2026-03-11 API (httpx.MockTransport).

Behaves like the documented API where the connector depends on it:
  - Notion-Version header required (a fixed set is served), Bearer token required (401),
  - pages, databases with `data_sources`, data-source query (POST) with a small page size
    so cursors are exercised, block children with cursors, append with validation
    (creatable types only, 2000-char runs, no response-only fields, tables need rows,
    columns need 2 children), DELETE = in_trash (never a hard delete),
  - a page the token was not given is a 404 `object_not_found` (`hidden`),
  - knobs for rate limiting (429 + Retry-After), a 503 that still committed the write
    (`committed_resource_id` / `committed_child_ids`), and failing reads.

Every request is logged in `log` as {"method", "path", "version", "body", "auth"} so tests
can assert what reached the wire.
"""

from __future__ import annotations

import json

import httpx

TOKEN = "ntn_FAKE_token_for_tests_0123456789"  # pregate: allow-secret offline fake tenant value
VERSIONS = {"2026-03-11", "2025-09-03", "2022-06-28"}
CREATABLE = {
    "embed", "bookmark", "image", "video", "pdf", "file", "audio", "code", "equation",
    "divider", "breadcrumb", "tab", "table_of_contents", "link_to_page", "table",
    "table_row", "column_list", "column", "heading_1", "heading_2", "heading_3",
    "heading_4", "paragraph", "bulleted_list_item", "numbered_list_item", "quote",
    "to_do", "toggle", "callout", "synced_block",
}  # fmt: skip
RUN_KEYS = {"type", "text", "annotations", "mention", "equation"}


class Killed(BaseException):
    """The process dies mid-run (power loss, OOM kill): raised from the transport, so no
    except-Exception handler in the connector or the flow engine can tidy up after it."""


def uid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def run(content, ann=None, link=None):
    a = {
        "bold": False,
        "italic": False,
        "strikethrough": False,
        "underline": False,
        "code": False,
        "color": "default",
    }
    a.update(ann or {})
    return {
        "type": "text",
        "text": {"content": content, "link": ({"url": link} if link else None)},
        "annotations": a,
        "plain_text": content,
        "href": link,
    }


def mention_page(page_id, title):
    return {
        "type": "mention",
        "mention": {"type": "page", "page": {"id": page_id}},
        "annotations": run("")["annotations"],
        "plain_text": title,
        "href": f"https://www.notion.so/{page_id.replace('-', '')}",
    }


def mention_user(user_id, name):
    return {
        "type": "mention",
        "mention": {
            "type": "user",
            "user": {"object": "user", "id": user_id, "name": name, "avatar_url": None},
        },
        "annotations": run("")["annotations"],
        "plain_text": "@" + name,
        "href": None,
    }


def equation(expr):
    return {
        "type": "equation",
        "equation": {"expression": expr},
        "annotations": run("")["annotations"],
        "plain_text": expr,
        "href": None,
    }


def block(btype, runs=None, **extra):
    inner = {"rich_text": runs if runs is not None else [], "color": "default"}
    inner.update(extra)
    return {"type": btype, btype: inner}


class FakeNotion:
    def __init__(self, token: str = TOKEN):
        self.token = token
        self.seq = 100
        self.pages: dict = {}  # id -> {title, parent, in_trash, last_edited_time}
        self.blocks: dict = {}  # id -> block dict (type + type object)
        self.kids: dict = {}  # parent id (page or block) -> ordered child ids
        self.databases: dict = {}  # id -> {"data_sources": [ids], "in_trash": bool}
        self.ds_rows: dict = {}  # data source id -> [page ids]
        self.log: list = []
        self.hidden: set = set()  # ids answering 404 (not shared with the integration)
        self.rate_limit_next = 0  # answer this many requests with 429 + Retry-After
        self.fail_reads: set = set()  # block ids whose children GET answers 500
        self.fail_post_pages = 0  # next N POST /pages answer 503 WITHOUT committing
        self.commit_post_pages = 0  # next N POST /pages answer 503 AFTER committing
        self.fail_append_once: set = set()  # parent ids whose next append answers 500
        self.commit_append_once: set = set()  # parent ids whose next append commits then 503s
        self.query_page_size = 2
        self.n_writes = 0  # POST / PATCH / DELETE requests seen (queries excluded)
        self.kill_at_write = 0  # raise Killed instead of serving write number N
        self.fail_at_write = 0  # an outage: write N and every later write answer 500
        self.fail_once_at_write = 0  # only write N answers 500 (nothing committed)
        self.root = self.add_page("Root", parent=None)

    # ---- setup helpers ----
    def nid(self) -> str:
        self.seq += 1
        return uid(self.seq)

    def add_page(self, title, parent="__root__", blocks=None, db_row_of=None) -> str:
        if parent == "__root__":
            parent = self.root
        pid = self.nid()
        if db_row_of:
            par = {
                "type": "data_source_id",
                "data_source_id": db_row_of[1],
                "database_id": db_row_of[0],
            }
            self.ds_rows.setdefault(db_row_of[1], []).append(pid)
        elif parent:
            par = {"type": "page_id", "page_id": parent}
        else:
            par = {"type": "workspace", "workspace": True}
        self.pages[pid] = {
            "title": title,
            "parent": par,
            "in_trash": False,
            "last_edited_time": "2026-02-01T00:00:00.000Z",
        }
        self.kids[pid] = []
        if parent:
            self.kids.setdefault(parent, []).append(("page", pid))
        for b in blocks or []:
            self.put_block(pid, b)
        return pid

    def add_database(self, title="DB", n_sources=1):
        did = self.nid()
        ds = []
        for _ in range(n_sources):
            d = self.nid()
            ds.append(d)
            self.ds_rows[d] = []
        self.databases[did] = {"data_sources": ds, "in_trash": False, "title": title}
        return did, ds

    def put_block(self, parent, b) -> str:
        """Store a block (with nested `children` lifted into the tree); returns its id."""
        bid = self.nid()
        typ = b["type"]
        inner = dict(b[typ])
        children = inner.pop("children", None)
        self.blocks[bid] = {
            "object": "block",
            "id": bid,
            "type": typ,
            typ: inner,
            "has_children": bool(children),
            "in_trash": False,
            "created_time": "2026-01-01T00:00:00.000Z",
            "last_edited_time": "2026-01-01T00:00:00.000Z",
        }
        self.kids.setdefault(parent, []).append(("block", bid))
        self.kids.setdefault(bid, [])
        for ch in children or []:
            self.put_block(bid, ch)
        self.blocks[bid]["has_children"] = bool(self.kids[bid])
        return bid

    def seed_preexisting_target(self, source, title) -> str:
        return self.add_page(title, parent=source)

    # ---- reading back ----
    def transport(self):
        return httpx.MockTransport(self._handle)

    def children_pages(self, parent):
        return [
            i for k, i in self.kids.get(parent, []) if k == "page" and not self.pages[i]["in_trash"]
        ]

    def by_title(self, title):
        return [i for i, p in self.pages.items() if p["title"] == title and not p["in_trash"]]

    def body(self, pid):
        """The live body tree of a page as nested (type, texts, children) tuples."""

        def walk(parent):
            out = []
            for kind, i in self.kids.get(parent, []):
                if kind != "block" or self.blocks[i]["in_trash"]:
                    continue
                b = self.blocks[i]
                inner = b[b["type"]]
                texts = [
                    (r.get("text") or {}).get("content", r.get("plain_text", ""))
                    for r in inner.get("rich_text", [])
                ]
                out.append((b["type"], texts, walk(i)))
            return out

        return walk(pid)

    def texts(self, pid):
        out = []

        def flat(items):
            for _t, texts, kids in items:
                out.extend(texts)
                flat(kids)

        flat(self.body(pid))
        return out

    def writes(self, method=None):
        return [
            e
            for e in self.log
            if e["method"] in ("POST", "PATCH", "DELETE")
            and not e["path"].endswith("/query")
            and (method is None or e["method"] == method)
        ]

    # ---- the HTTP surface ----
    def _err(self, status, code, msg="", headers=None):
        return httpx.Response(
            status,
            json={"object": "error", "status": status, "code": code, "message": msg},
            headers=headers,
        )

    def _visible(self, i) -> bool:
        return i not in self.hidden

    def _page_json(self, pid):
        p = self.pages[pid]
        return {
            "object": "page",
            "id": pid,
            "in_trash": p["in_trash"],
            "is_archived": p["in_trash"],
            "parent": p["parent"],
            "last_edited_time": p["last_edited_time"],
            "properties": {"title": {"id": "title", "type": "title", "title": [run(p["title"])]}},
        }

    def _block_json(self, bid):
        b = dict(self.blocks[bid])
        b["has_children"] = any(
            not (k == "block" and self.blocks[i]["in_trash"]) for k, i in self.kids.get(bid, [])
        )
        return b

    def _validate_runs(self, arr, where):
        if not isinstance(arr, list):
            return f"{where} must be an array"
        if len(arr) > 100:
            return f"{where} should be <= 100 elements"
        for r in arr:
            extra = set(r) - RUN_KEYS
            if extra:
                return f"{where}: unexpected fields {sorted(extra)}"
            if r.get("type") == "text":
                c = (r.get("text") or {}).get("content")
                if not isinstance(c, str) or len(c) > 2000:
                    return f"{where}: text.content must be <= 2000 characters"
            if r.get("type") == "mention":
                m = r.get("mention") or {}
                body = m.get(m.get("type")) or {}
                if set(body) - {"id", "start", "end", "time_zone", "url"}:
                    return f"{where}: mention carries read-only fields {sorted(body)}"
        return None

    def _validate_block(self, b, top=True):
        t = b.get("type")
        if t not in CREATABLE:
            return f"body.children[0].type should be a creatable block, instead was {t!r}"
        if set(b) - {"type", t}:
            return f"unexpected block fields {sorted(set(b) - {'type', t})}"
        inner = b.get(t) or {}
        for key in ("rich_text", "caption"):
            if key in inner:
                p = self._validate_runs(inner[key], f"{t}.{key}")
                if p:
                    return p
        for cell in inner.get("cells") or []:
            p = self._validate_runs(cell, f"{t}.cells")
            if p:
                return p
        if t == "table" and not inner.get("children"):
            return "table.children should be defined"
        if t == "column_list":
            cols = inner.get("children") or []
            if len(cols) < 2 or not all((c.get("column") or {}).get("children") for c in cols):
                return "column_list needs 2 columns with children"
        if t in ("image", "file", "pdf", "video", "audio") and inner.get("type") in (
            "file",
            "file_upload",
        ):
            return "Cannot create a file block with a Notion-hosted file"
        if t == "synced_block" and inner.get("synced_from") and inner.get("children"):
            return "a synced block duplicate cannot have children"
        if len(inner.get("children") or []) > 100:
            return f"{t}.children should be <= 100 elements"
        for k in inner.get("children") or []:
            p = self._validate_block(k, top=False)
            if p:
                return p
        return None

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        try:
            body = json.loads(req.content) if req.content else None
        except ValueError:
            body = None
        version = req.headers.get("notion-version")
        auth = req.headers.get("authorization", "")
        self.log.append(
            {
                "method": method,
                "path": path,
                "version": version,
                "body": body,
                "auth": auth,
                "query": dict(req.url.params),
            }
        )
        if method in ("POST", "PATCH", "DELETE") and not path.endswith("/query"):
            self.n_writes += 1
            if self.kill_at_write and self.n_writes == self.kill_at_write:
                raise Killed(f"killed before write {self.n_writes}: {method} {path}")
            if (self.fail_at_write and self.n_writes >= self.fail_at_write) or (
                self.fail_once_at_write and self.n_writes == self.fail_once_at_write
            ):
                return self._err(500, "internal_server_error", "boom")
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            return self._err(429, "rate_limited", "slow down", headers={"Retry-After": "1"})
        if auth != f"Bearer {self.token}":
            return self._err(401, "unauthorized", "API token is invalid.")
        if version not in VERSIONS:
            return self._err(400, "invalid_request", "Notion-Version header is required")
        seg = [p for p in path.split("/") if p]
        # seg[0] == "v1"
        if seg[:2] == ["v1", "users"] and seg[2:] == ["me"]:
            return httpx.Response(
                200, json={"object": "user", "id": uid(1), "type": "bot", "name": "LocQA bot"}
            )
        if seg[:2] == ["v1", "pages"]:
            return self._pages(req, seg, body)
        if seg[:2] == ["v1", "databases"] and method == "GET" and len(seg) == 3:
            did = seg[2]
            if did not in self.databases or did in self.hidden:
                return self._err(404, "object_not_found", "Could not find database")
            d = self.databases[did]
            out = {
                "object": "database",
                "id": did,
                "in_trash": d["in_trash"],
                "parent": {"type": "workspace", "workspace": True},
            }
            if version >= "2025-09-03":
                out["data_sources"] = [
                    {"id": s, "name": f"Source {n}"} for n, s in enumerate(d["data_sources"])
                ]
            return httpx.Response(200, json=out)
        if seg[:2] == ["v1", "data_sources"] and method == "POST" and seg[3:] == ["query"]:
            return self._query(seg[2], body or {})
        if seg[:2] == ["v1", "blocks"]:
            return self._blocks(req, seg, body)
        return self._err(404, "invalid_request_url", path)

    def _pages(self, req, seg, body):
        method = req.method
        if method == "POST" and len(seg) == 2:
            if self.fail_post_pages > 0:
                self.fail_post_pages -= 1
                return self._err(503, "service_unavailable", "try later")
            par = (body or {}).get("parent") or {}
            if "page_id" not in par and "data_source_id" not in par:
                return self._err(
                    400,
                    "validation_error",
                    "internal connections need a page or data source parent",
                )
            if "page_id" in par:
                pid = par["page_id"]
                if pid not in self.pages or pid in self.hidden or self.pages[pid]["in_trash"]:
                    return self._err(404, "object_not_found", "parent not shared")
            props = (body or {}).get("properties") or {}
            if set(props) - {"title"}:
                return self._err(
                    400, "validation_error", "under a page parent, title is the only property"
                )
            problem = self._validate_runs((props.get("title") or {}).get("title"), "title")
            if problem:
                return self._err(400, "validation_error", problem)
            title = "".join(
                (r.get("text") or {}).get("content", "") for r in props["title"]["title"]
            )
            new = self.add_page(title, parent=par["page_id"])
            if self.commit_post_pages > 0:
                self.commit_post_pages -= 1
                return httpx.Response(
                    503,
                    json={
                        "object": "error",
                        "status": 503,
                        "code": "service_unavailable",
                        "message": "committed",
                        "additional_data": {"committed_resource_id": new},
                    },
                )
            return httpx.Response(200, json=self._page_json(new))
        pid = seg[2] if len(seg) > 2 else ""
        if pid not in self.pages or pid in self.hidden:
            return self._err(404, "object_not_found", "Could not find page")
        if method == "GET":
            return httpx.Response(200, json=self._page_json(pid))
        if method == "PATCH":
            props = (body or {}).get("properties") or {}
            if "title" in props:
                problem = self._validate_runs((props["title"] or {}).get("title"), "title")
                if problem:
                    return self._err(400, "validation_error", problem)
                self.pages[pid]["title"] = "".join(
                    (r.get("text") or {}).get("content", "") for r in props["title"]["title"]
                )
            if (body or {}).get("in_trash") is not None:
                self.pages[pid]["in_trash"] = bool(body["in_trash"])
            return httpx.Response(200, json=self._page_json(pid))
        return self._err(405, "invalid_request", "method")

    def _query(self, ds, body):
        rows = [
            r
            for r in self.ds_rows.get(ds, [])
            if r not in self.hidden and not self.pages[r]["in_trash"]
        ]
        if ds not in self.ds_rows:
            return self._err(404, "object_not_found", "data source")
        start = int(body.get("start_cursor") or 0)
        size = min(int(body.get("page_size") or 100), self.query_page_size)
        chunk = rows[start : start + size]
        more = start + size < len(rows)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "results": [self._page_json(r) for r in chunk],
                "has_more": more,
                "next_cursor": str(start + size) if more else None,
                "type": "page_or_data_source",
            },
        )

    def _blocks(self, req, seg, body):
        method = req.method
        if len(seg) == 3 and method == "DELETE":
            bid = seg[2]
            if bid in self.blocks:
                self.blocks[bid]["in_trash"] = True
                return httpx.Response(200, json={"object": "block", "id": bid, "in_trash": True})
            if bid in self.pages:
                self.pages[bid]["in_trash"] = True
                return httpx.Response(200, json={"object": "page", "id": bid, "in_trash": True})
            return self._err(404, "object_not_found", "block")
        if len(seg) == 4 and seg[3] == "children":
            parent = seg[2]
            if parent in self.hidden or (parent not in self.pages and parent not in self.blocks):
                return self._err(
                    404,
                    "object_not_found",
                    "Could not find block. Make sure the pages are shared with your connection.",
                )
            if method == "GET":
                if parent in self.fail_reads:
                    return self._err(500, "internal_server_error", "boom")
                items = []
                for kind, i in self.kids.get(parent, []):
                    if kind == "block" and not self.blocks[i]["in_trash"]:
                        items.append(self._block_json(i))
                    elif kind == "page" and not self.pages[i]["in_trash"]:
                        items.append(
                            {
                                "object": "block",
                                "id": i,
                                "type": "child_page",
                                "child_page": {"title": self.pages[i]["title"]},
                                "has_children": True,
                                "in_trash": False,
                                "last_edited_time": self.pages[i]["last_edited_time"],
                            }
                        )
                start = int(req.url.params.get("start_cursor") or 0)
                size = min(int(req.url.params.get("page_size") or 100), 100)
                chunk = items[start : start + size]
                more = start + size < len(items)
                return httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "results": chunk,
                        "has_more": more,
                        "next_cursor": str(start + size) if more else None,
                    },
                )
            if method == "PATCH":
                if set(body or {}) - {"children", "position", "notifications"}:
                    return self._err(
                        400, "validation_error", f"unexpected body fields {sorted(set(body))}"
                    )
                if "after" in (body or {}):
                    return self._err(400, "validation_error", "after is no longer accepted")
                kids = (body or {}).get("children")
                if not isinstance(kids, list) or not kids or len(kids) > 100:
                    return self._err(400, "validation_error", "children must hold 1 to 100 blocks")
                for b in kids:
                    problem = self._validate_block(b)
                    if problem:
                        return self._err(400, "validation_error", problem)

                def _count(x):
                    return 1 + sum(
                        _count(k) for k in (x.get(x["type"]) or {}).get("children") or []
                    )

                if sum(_count(b) for b in kids) > 1000 or len(req.content) > 500_000:
                    return self._err(400, "validation_error", "payload over 1000 blocks / 500 KB")
                if parent in self.fail_append_once:
                    self.fail_append_once.discard(parent)
                    return self._err(500, "internal_server_error", "boom")
                created = [self.put_block(parent, b) for b in kids]
                if parent in self.commit_append_once:
                    self.commit_append_once.discard(parent)
                    return httpx.Response(
                        503,
                        json={
                            "object": "error",
                            "status": 503,
                            "code": "service_unavailable",
                            "message": "committed",
                            "additional_data": {"committed_child_ids": created},
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "results": [self._block_json(i) for i in created],
                        "has_more": False,
                        "next_cursor": None,
                    },
                )
        return self._err(404, "invalid_request_url", "/".join(seg))
