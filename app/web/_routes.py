"""Inspect effective routes on both flattened and included-router FastAPI releases.

Mounts remain visible as mounts: API mirror guards reject them separately.
Never descend into their ASGI apps or confuse their paths with router includes.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from copy import copy


def walk_routes(app):
    """Yield route objects with effective prefixes, dependencies and schema flags.

    FastAPI 0.137+ owns include-context composition. Use its effective candidates
    rather than reconstructing prefixes from the original router. Copy contexts
    onto the original route type so existing Route/APIRoute predicates still work.
    Older FastAPI already exposes flattened routes and needs no adaptation.
    """

    def visit(routes):
        for route in routes:
            candidates = getattr(route, "effective_candidates", None)
            if candidates is not None:
                yield from visit(candidates())
            elif hasattr(route, "original_route"):
                effective = copy(route.starlette_route or route.original_route)
                if route.starlette_route is None:
                    effective.__dict__.update(vars(route))
                yield effective
            else:
                yield route

    yield from visit(app.routes)


def real_api_routes(app) -> set[tuple[str, str]]:
    """Every served /api method/path, including routes hidden from OpenAPI."""
    return {
        (method, route.path)
        for route in walk_routes(app)
        if (getattr(route, "path", "") or "").startswith("/api")
        for method in getattr(route, "methods", None) or ()
        if method not in {"HEAD", "OPTIONS"}
    }
