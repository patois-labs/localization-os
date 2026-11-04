"""Route pre-filter (S7b round 2, ADR 0103): same routing answers, far fewer route checks.

app/web/route_prefilter.py lets an included router answer NONE at once when none of its routes
can match the request's first path segment. This suite builds a concrete request path for EVERY
registered route (its include prefix applied, path parameters filled in) plus near misses (first
segment changed, trailing slash flipped, one segment cut, the root, unknown paths), and for each
path and several methods compares, router by router, the (Match, child scope endpoint) answer with
the filter installed against FastAPI's own answer. Then it checks the cost the filter exists for:
route checks per request for a hot /api path drop by an order of magnitude. A third check: an
unknown FastAPI shape makes install() refuse instead of patching.

Offline, hermetic (throwaway LOC_TMS_DATA_DIR). Final line: ROUTE PREFILTER OK. ASCII only.
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_routepre_")

from fastapi import routing as fr
from starlette import routing as sr

from app.main import app
from app.web import route_prefilter


def _leaf_paths():
    """Every route's full path (include prefix applied), in registration order."""
    out = []

    def walk_included(ir):
        for cand in ir.effective_candidates():
            if isinstance(cand, fr._IncludedRouter):
                walk_included(cand)
            elif isinstance(cand.original_route, fr.APIRoute):
                out.append(ir.include_context.path_for(cand.original_route))
            elif getattr(cand.starlette_route, "path", None) is not None:
                out.append(cand.starlette_route.path)

    for r in app.router.routes:
        if isinstance(r, fr._IncludedRouter):
            walk_included(r)
        elif getattr(r, "path", None) is not None:
            out.append(r.path)
    return out


def _concrete(path):
    path = re.sub(r"\{[^}:]+:path\}", "a/b", path)
    return re.sub(r"\{[^}]+\}", "1", path) or "/"


def _variants(path):
    yield path
    yield path.rstrip("/") + "/" if not path.endswith("/") else path.rstrip("/") or "/"
    parts = path.split("/")
    if len(parts) > 2:
        yield "/".join(parts[:-1]) or "/"
        yield "/" + parts[1] + "x/" + "/".join(parts[2:])
        yield "/x" + parts[1] + "/" + "/".join(parts[2:])


def _scope(path, method, kind="http", root=""):
    return {
        "type": kind,
        "path": root + path,
        "root_path": root,
        "method": method,
        "headers": [],
        "query_string": b"",
    }


def _answers(path, method, kind="http", root=""):
    scope = _scope(path, method, kind, root)
    out = []
    for r in app.router.routes:
        match, child = r.matches(dict(scope))
        out.append((match, getattr(child.get("endpoint"), "__qualname__", None)))
    return out


def test_same_answers():
    paths = []
    for p in _leaf_paths():
        c = _concrete(p)
        paths.extend(_variants(c))
    paths += ["/", "/nope", "/api", "/api/", "/api/nope/1", "/static/x.css", "/health", "/ready"]
    paths = list(dict.fromkeys(paths))
    methods = ("GET", "POST", "PATCH", "DELETE")
    # (path, method, scope type, root_path): every method over http, plus (gate round 2 LOW-2)
    # a trailing newline (route regexes end in `$`), a websocket scope and a root_path prefix.
    cases = [(p, m, "http", "") for p in paths for m in methods]
    cases += [(p + "\n", "GET", "http", "") for p in paths]
    cases += [(p, "GET", "websocket", "") for p in paths]
    cases += [(p, "GET", "http", "/prefix") for p in paths]
    route_prefilter.uninstall()
    want = {c: _answers(*c) for c in cases}
    assert route_prefilter.install(), "install() refused on the pinned FastAPI"
    diffs = [c for c in cases if _answers(*c) != want[c]]
    assert not diffs, f"{len(diffs)} routing differences, first: {diffs[:5]}"
    fulls = sum(1 for c in cases if any(a[0].name == "FULL" for a in want[c]))
    print(f"  {len(cases)} cases ({len(paths)} paths; methods, newline, websocket, root_path):")
    print(f"  identical per-router answers ({fulls} cases matched a route)")


def test_fewer_route_checks():
    calls = {"n": 0}
    real = fr.APIRoute.matches

    def counting(self, scope):
        calls["n"] += 1
        return real(self, scope)

    fr.APIRoute.matches = counting
    try:
        route_prefilter.uninstall()
        for r in app.router.routes:
            r.matches(_scope("/api/jobs/1", "GET"))
        before = calls["n"]
        calls["n"] = 0
        route_prefilter.install()
        for r in app.router.routes:
            r.matches(_scope("/api/jobs/1", "GET"))
        after = calls["n"]
    finally:
        fr.APIRoute.matches = real
    assert after * 3 <= before, (before, after)
    print(f"  /api/jobs/1: {before} route checks without the filter, {after} with it")


def test_refuses_unknown_shape():
    route_prefilter.uninstall()
    saved = fr._IncludedRouter.effective_candidates
    try:
        del fr._IncludedRouter.effective_candidates
        assert route_prefilter.install() is False
        assert not getattr(fr._IncludedRouter.matches, "_locos_route_prefilter", False)
    finally:
        fr._IncludedRouter.effective_candidates = saved
    assert route_prefilter.install()
    print("  unknown FastAPI internals: install() refuses and patches nothing")


def test_route_kinds_covered():
    kinds = {type(r).__name__ for r in app.router.routes}
    assert "_IncludedRouter" in kinds, kinds
    assert any(isinstance(r, sr.Mount) for r in app.router.routes), "expected the static mount"
    print(f"  top-level route kinds: {sorted(kinds)}")


def main() -> None:
    test_route_kinds_covered()
    test_same_answers()
    test_fewer_route_checks()
    test_refuses_unknown_shape()
    print("ROUTE PREFILTER OK")


if __name__ == "__main__":
    main()
