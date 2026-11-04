"""Route pre-filter: skip an included router that cannot match the request's first path segment.

S7b round 2 (ADR 0103). FastAPI 0.142 keeps every `app.include_router(...)` as an
`_IncludedRouter` and matches a request by asking each one in turn; an included router answers
by matching every route under it (nested routers too), and the router that matches is matched
again when it handles the request. With about 40 included routers and 1,000 routes, a plain
/api request ran about 580 route checks, roughly a quarter of the web role's CPU per
translate job.

The filter: for each included router, the literal path prefix (up to two segments) of every
route under it, include prefix applied: `/api/jobs/{id}` gives ("api", "jobs"), `/api/{x}` gives
("api",). A route whose FIRST segment is a parameter, a mount, a host route or a frontend route
group makes the router "anything" and it is never skipped. A route regex anchors each literal
segment between "/" and "/" or the end, so a request can match a route only if the route's
literal prefix is a prefix of the request's segments; when no route of the router passes that
test, `matches` answers NONE without walking it: neither FULL nor PARTIAL (a PARTIAL also needs
the path to match). Everything else is FastAPI's own code, in its own order, so the first match and
every response are what they were. The sets are recomputed when a router's routes change
(`_get_routes_version`).

S9 (WP-FW): `APIRouter._get_routes_version()` walks the whole router tree below the router on
EVERY call, and matching a request asks it about 270 times (the pre-filter once per candidate
router, FastAPI's own candidate cache twice more, then again when the winner handles the request).
After startup the answer never changes, so `install()` also memoizes it per router: the cached
value is valid while no `_mark_routes_changed` has run anywhere (a process-wide generation
counter) and the router's own route count is the same. That is exactly the set of events that
change the answer today (every route add, include and router include goes through
`_mark_routes_changed`; a version is the sum of those counters over the tree), plus a coarser
trigger: ANY router's change invalidates every cache. Only the top-level call is cached; FastAPI's
recursive calls (which pass `seen`) run its own code. A direct edit of `router.routes` that
bypasses `_mark_routes_changed` is invisible to FastAPI's own caches too (nothing recomputes
`effective_candidates` for it), so it is equally invisible here.

`install()` patches `fastapi.routing._IncludedRouter.matches` once per process and refuses (logs,
patches nothing) when the FastAPI internals it needs are not there, so a FastAPI upgrade can
only lose the speed-up, never change routing. `verify_route_prefilter` checks every registered
route and a set of near-miss paths against the unpatched answer.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from __future__ import annotations

import logging

log = logging.getLogger("loc_tms.route_prefilter")

_ATTR = "_locos_route_heads"
_MARK = "_locos_route_prefilter"
_VERSION_ATTR = "_locos_routes_version"
_VERSION_MARK = "_locos_routes_version_cache"

#: Bumped by every `APIRouter._mark_routes_changed` (the one door FastAPI's own route cache
#: invalidation goes through). The cached `_get_routes_version` below is valid while it is
#: unchanged.
_generation = 0


def _key(path: str):
    """The literal prefix (up to two segments) of a route path, or None when its first segment
    can be anything."""
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    key = []
    for seg in path[1:].split("/")[:2]:
        if "{" in seg or "}" in seg:
            break
        key.append(seg)
    return tuple(key) or None


def request_segments(path: str) -> list:
    """The first two segments of a request path ("/" gives [""]). One trailing newline is
    dropped first: a route regex ends in `$`, which also matches before a final newline, so
    FastAPI matches "/health" plus a newline to "/health" and the filter must too (gate
    LOW-1)."""
    if path.endswith("\n"):
        path = path[:-1]
    return (path[1:] if path.startswith("/") else path).split("/", 2)[:2]


#: The last (path, root_path, segments) computed: a request is matched against dozens of
#: included routers in a row, all with the same scope path. One tuple, replaced whole, so a
#: thread switch can never leave it half written.
_segments_memo = (None, None, None)


def _segments_for(scope, get_route_path) -> list:
    """`request_segments(get_route_path(scope))`, computed once per distinct (path, root_path)."""
    global _segments_memo
    path = scope["path"]
    root_path = scope.get("root_path", "")
    memo = _segments_memo
    if memo[0] == path and memo[1] == root_path:
        return memo[2]
    segs = request_segments(get_route_path(scope))
    _segments_memo = (path, root_path, segs)
    return segs


def could_match(keys, segs) -> bool:
    """Whether some route key is a prefix of the request's segments (over-inclusive)."""
    one, two = keys
    if (segs[0],) in one:
        return True
    return len(segs) > 1 and (segs[0], segs[1]) in two


def heads_for(included):
    """(one-segment keys, two-segment keys) of every route under `included`, or None when some
    route can start with anything (then the router is never skipped). Cached per routes
    version on the instance."""
    version = included.original_router._get_routes_version()
    cached = included.__dict__.get(_ATTR)
    if cached is not None and cached[0] == version:
        return cached[1]
    from fastapi import routing as fr
    from starlette import routing as sr

    heads: set | None = set()
    for cand in included.effective_candidates():
        if isinstance(cand, fr._IncludedRouter):
            sub = heads_for(cand)
            if sub is None:
                heads = None
                break
            heads |= sub[0] | sub[1]
            continue
        orig = cand.original_route
        if isinstance(orig, fr.APIRoute):
            path = included.include_context.path_for(orig)
        elif isinstance(cand.starlette_route, (sr.Route, sr.WebSocketRoute)):
            path = cand.starlette_route.path
        else:  # Mount, Host, frontend route group, anything new: never skip this router
            heads = None
            break
        key = _key(path)
        if key is None:
            heads = None
            break
        heads.add(key)
    frozen = None
    if heads is not None:
        frozen = (
            frozenset(k for k in heads if len(k) == 1),
            frozenset(k for k in heads if len(k) == 2),
        )
    included.__dict__[_ATTR] = (version, frozen)
    return frozen


def install() -> bool:
    """Patch `_IncludedRouter.matches` with the pre-filter (idempotent). False when this
    FastAPI lacks what the filter relies on (nothing is patched then)."""
    try:
        from fastapi import routing as fr
        from starlette.routing import Match

        from app.web.route_path import route_path as get_route_path  # the one private-API wrapper

        cls = fr._IncludedRouter
        for name in ("matches", "effective_candidates"):
            if not callable(getattr(cls, name, None)):
                raise AttributeError(name)
        for name in ("include_context", "original_router"):
            if name not in getattr(cls, "__dataclass_fields__", {}):
                raise AttributeError(name)
        if not callable(getattr(fr._RouterIncludeContext, "path_for", None)):
            raise AttributeError("path_for")
        if not callable(getattr(fr.APIRouter, "_get_routes_version", None)):
            raise AttributeError("_get_routes_version")
        if not callable(getattr(fr.APIRouter, "_mark_routes_changed", None)):
            raise AttributeError("_mark_routes_changed")
        probe = fr.APIRouter()
        if probe._get_routes_version() != probe._routes_version:
            raise AttributeError("_routes_version")
    except Exception:
        log.info("route pre-filter not installed: FastAPI internals differ", exc_info=True)
        return False
    original = cls.matches
    if getattr(original, _MARK, False):
        return True
    _install_version_cache(fr.APIRouter)

    def matches(self, scope):
        if scope.get("type") in ("http", "websocket"):
            try:
                heads = heads_for(self)
            except Exception:
                heads = None  # any surprise: match exactly as FastAPI does
            if heads is not None and not could_match(heads, _segments_for(scope, get_route_path)):
                return Match.NONE, {}
        return original(self, scope)

    setattr(matches, _MARK, True)
    matches.__wrapped__ = original
    cls.matches = matches
    return True


def _install_version_cache(router_cls) -> None:
    """Memoize `router_cls._get_routes_version()` (top-level calls only), invalidated by
    every `_mark_routes_changed`. Idempotent."""
    original_version = router_cls._get_routes_version
    if getattr(original_version, _VERSION_MARK, False):
        return
    original_mark = router_cls._mark_routes_changed

    def _mark_routes_changed(self):
        # Bump AFTER the counter changed (gate round 1 LOW): a version computed between a
        # bump-first and the counter write would be cached under the new generation with the
        # old counter, and stay stale until the next change anywhere.
        global _generation
        try:
            return original_mark(self)
        finally:
            _generation += 1

    def _get_routes_version(self, seen=None):
        if seen is not None:  # FastAPI's own recursion: its code, its shared `seen` set
            return original_version(self, seen)
        cached = self.__dict__.get(_VERSION_ATTR)
        generation = _generation  # read BEFORE computing: a change during it is not lost
        count = len(self.routes)
        if cached is not None and cached[0] == generation and cached[1] == count:
            return cached[2]
        version = original_version(self)
        self.__dict__[_VERSION_ATTR] = (generation, count, version)
        return version

    _mark_routes_changed.__wrapped__ = original_mark
    _get_routes_version.__wrapped__ = original_version
    setattr(_get_routes_version, _VERSION_MARK, True)
    router_cls._mark_routes_changed = _mark_routes_changed
    router_cls._get_routes_version = _get_routes_version


def uninstall() -> None:
    """Restore FastAPI's own `matches` and route version (tests)."""
    from fastapi import routing as fr

    current = fr._IncludedRouter.matches
    if getattr(current, _MARK, False):
        fr._IncludedRouter.matches = current.__wrapped__
    version = fr.APIRouter._get_routes_version
    if getattr(version, _VERSION_MARK, False):
        fr.APIRouter._get_routes_version = version.__wrapped__
        fr.APIRouter._mark_routes_changed = fr.APIRouter._mark_routes_changed.__wrapped__
